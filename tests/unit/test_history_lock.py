"""History lock tests (Phase 4, Story 4.6 / AD-31).

flock is cross-process: threads alone prove nothing, so serialization and
blocking-waiter tests use real processes (fork). Typed-error coverage spans
both the mutex (non-blocking acquire) and the writer (append I/O failure,
which must not hang). Output is validated through the repo's tolerant
history reader (``InspectHistoryUseCase``), not raw ``json.loads``.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest

from runtime.adapters.flock_seed_mutex import FlockHistoryMutex
from runtime.adapters.seeder import CacheSeeder
from runtime.application.inspect import InspectHistoryUseCase
from runtime.domain.models import HistoryLockError

HISTORY_LOCK = ".history.lock"
_HEX = "0123456789abcdef"


def _worker_hash(worker: int, i: int) -> str:
    return f"{worker:02x}{i:062x}"[-64:]


def _append_many(state_root: str, worker: int, count: int) -> None:
    seeder = CacheSeeder(Path(state_root))
    for i in range(count):
        seeder.append_history(
            trigger="set",
            wallpaper_hash=_worker_hash(worker, i),
            source_path=f"/img/{worker}-{i}.png",
        )


def test_concurrent_processes_serialize_lossless(tmp_path: Path) -> None:
    workers, per_worker = 4, 25
    state_root = tmp_path / "state"
    state_root.mkdir()
    with mp.get_context("fork").Pool(workers) as pool:
        pool.starmap(_append_many, [(str(state_root), w, per_worker) for w in range(workers)])
    records = InspectHistoryUseCase(state_root).run(limit=0)
    assert len(records) == workers * per_worker
    assert sorted(r.wallpaper for r in records) == sorted(
        _worker_hash(w, i) for w in range(workers) for i in range(per_worker)
    )


def test_heal_race_under_contention_lossless(tmp_path: Path) -> None:
    """A torn tail healed while appenders run must not lose/interleave lines."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    history = state_root / "history.jsonl"
    history.write_bytes(b'{"ts":"2026-09-10T00:00:00Z","trigger":"set","wallp')  # torn tail
    seeder = CacheSeeder(state_root)
    with mp.get_context("fork").Pool(3) as pool:
        for _ in range(2):
            pool.apply_async(_append_many, (str(state_root), 7, 10))
        pool.apply_async(CacheSeeder(Path(state_root)).heal_torn_history_tail)
        pool.close()
        pool.join()
    # No exception escaped the pool; every appended + healed line is intact.
    records = InspectHistoryUseCase(state_root).run(limit=0)
    assert len(records) == 20
    # A second heal is a no-op (nothing torn remains).
    assert seeder.heal_torn_history_tail() is None


def test_lock_held_nonblocking_raises_typed_error(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    lock_path = state_root / HISTORY_LOCK
    with FlockHistoryMutex(lock_path).hold(blocking=True):
        with pytest.raises(HistoryLockError, match="history lock acquire failed"):
            with FlockHistoryMutex(lock_path).hold(blocking=False):
                raise AssertionError("must not acquire a held lock")


def test_append_io_failure_raises_typed_error(tmp_path: Path) -> None:
    """Writer-level AC2 proof: append I/O failure is typed, never hangs."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "history.jsonl").mkdir()  # open(O_APPEND|O_NOFOLLOW) fails
    with pytest.raises(HistoryLockError, match="history append open failed"):
        CacheSeeder(state_root).append_history(trigger="set", wallpaper_hash="ab" * 32)


def _blocking_appender(state_root: str) -> None:
    CacheSeeder(Path(state_root)).append_history(trigger="set", wallpaper_hash="ab" * 32)


def test_blocking_appender_waits_then_proceeds_cross_process(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    lock_path = state_root / HISTORY_LOCK
    ctx = mp.get_context("fork")
    proc = ctx.Process(target=_blocking_appender, args=(str(state_root),))
    with FlockHistoryMutex(lock_path).hold(blocking=True):
        proc.start()
        time.sleep(0.3)
        assert proc.is_alive()  # still waiting, not failed
    proc.join(timeout=10)
    assert proc.exitcode == 0
    records = InspectHistoryUseCase(state_root).run(limit=0)
    assert len(records) == 1 and records[0].trigger == "set"
