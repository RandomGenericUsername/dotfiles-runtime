"""Generations v3 acceptance tests (REQ-020).

The generation layout, one commit pointer, the daemon queue's coalescing
policy, generation-bound acks, locality, event-driven waits and the
desired/committed/applied inspection projection.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from runtime.adapters.converge_lock import ConvergeLock
from runtime.adapters.converge_queue import ConvergeJobQueue
from runtime.adapters.converge_types import ConvergeQueueResult
from runtime.adapters.generation_store import (
    GenerationSpec,
    GenerationStore,
    generation_intent_hash,
)

GEN = "a" * 64


def test_manifest_layout_and_single_pointer(tmp_path: Path) -> None:
    from tests.unit.test_doctor_check import _seed_state

    state_root = tmp_path / "state"
    _seed_state(state_root)
    store = GenerationStore(state_root)
    generation = store.current_hash()
    assert generation is not None
    root = store.generation_path(generation)
    manifest = store.verify(generation)
    assert manifest["version"] == 3
    assert manifest["generationHash"] == generation
    assert manifest["layers"]["palette"]["path"] == "palette"
    assert (root / "palette" / "colors.yaml").is_file()
    assert (root / "appearance" / "tokens.json").is_file()
    assert (root / "appearance" / "ags.css").is_file()
    assert (root / "wallpaper" / "wallpaper.png").is_symlink()
    assert (root / "colors.yaml").is_symlink()
    # current is exactly one symlink; no per-family directory remains.
    assert (state_root / "current").is_symlink()
    assert os.readlink(state_root / "current") == f"generations/{generation}"
    assert (state_root / "current").is_symlink()
    # no staging left behind
    assert not list(store.generations_root.glob(".staging-*"))


def test_queue_coalesces_identical_and_supersedes_queued(tmp_path: Path) -> None:
    from runtime.adapters.in_process_hub import InProcessJobRegistry
    from runtime.domain.hub import EventHub

    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    events: list[object] = []

    import time

    registry = InProcessJobRegistry(
        EventHub(
            epoch=1,
            clock=time.monotonic,
            id_factory=lambda: __import__("uuid").uuid4().hex,
            sink=events.append,
        )
    )

    def converge(intent_hash: str, job_id: str) -> ConvergeQueueResult:
        calls.append(intent_hash)
        if len(calls) == 1:
            started.set()
            release.wait(timeout=5)
        return ConvergeQueueResult(generation_hash=str(len(calls)) * 64, exit_code=0)

    queue = ConvergeJobQueue(
        registry=registry,
        state_root=tmp_path,
        converge=converge,
        registry_lock=threading.Lock(),
    )
    queue.start()
    try:
        first = queue.submit("converge:" + "1" * 64, 60)
        assert first is not None
        assert started.wait(timeout=5)
        # Same intent while running coalesces into the running job.
        same = queue.submit("converge:" + "1" * 64, 60)
        # Two different queued intents: the newest supersedes the older.
        second = queue.submit("converge:" + "2" * 64, 60)
        third = queue.submit("converge:" + "3" * 64, 60)
        assert same and second and third
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(calls) < 2:
            time.sleep(0.01)
        assert calls == ["1" * 64, "3" * 64]
        # Every waiter id received a result and a JobFinished.
        for job_id in (first, same, second, third):
            assert (tmp_path / "converge-results" / f"{job_id}.json").is_file()
        finished = [event for event in events if getattr(event, "event", "") == "job_finished"]
        assert {event.job_id for event in finished} >= {first, same, second, third}
    finally:
        queue.stop()


def test_concurrent_converge_lock_is_exclusive(tmp_path: Path) -> None:
    lock = ConvergeLock(tmp_path)
    entered: list[int] = []
    finished: list[int] = []

    def worker(index: int) -> None:
        with lock.hold():
            entered.append(index)
            finished.append(index)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(entered) == [0, 1, 2, 3]
    assert sorted(finished) == [0, 1, 2, 3]


def test_generation_bound_ack_mismatch_is_refused(tmp_path: Path) -> None:
    from tests.unit.test_ags_app_refresh import (
        GEN as ACK_GEN,
        _LiveRunner,
        _coordinator,
        _deploy_live,
        _process,
        _state,
    )

    spine = tmp_path / "install"
    link = _deploy_live(spine)
    stale = "b" * 64
    runner = _LiveRunner({"clipboard": [(json.dumps({"applied": stale}) + "\n",)]})
    coordinator = _coordinator(
        tmp_path,
        live_runner=runner,
        live_names=("clipboard",),
        processes=(_process(42, link, "clipboard"),),
    )
    outcome = coordinator.refresh(_state(), generation_hash=ACK_GEN)
    assert outcome.records[0].status == "deferred"
    assert outcome.records[0].status != "applied"
    assert "stale ack" in outcome.records[0].reason
    assert outcome.failures


def test_boolean_v2_ack_is_never_applied(tmp_path: Path) -> None:
    from tests.unit.test_ags_app_refresh import (
        GEN as ACK_GEN,
        _LiveRunner,
        _coordinator,
        _deploy_live,
        _process,
        _state,
    )

    spine = tmp_path / "install"
    link = _deploy_live(spine)
    runner = _LiveRunner({"clipboard": [('{"ok":true}\n',)]})
    coordinator = _coordinator(
        tmp_path,
        live_runner=runner,
        live_names=("clipboard",),
        processes=(_process(42, link, "clipboard"),),
    )
    outcome = coordinator.refresh(_state(), generation_hash=ACK_GEN)
    assert outcome.records[0].status == "failed"
    assert outcome.records[0].status != "applied"


def test_no_runtime_writes_inside_an_app_tag(tmp_path: Path) -> None:
    from runtime.adapters.seeder import CacheSeeder

    spine = tmp_path / "install"
    app = spine / "apps" / "bar" / "v1.0.0"
    app.mkdir(parents=True)
    (app / "app.tsx").write_text('app.start({ instanceName: "ags" })')
    (spine / "config").mkdir()
    (spine / "config" / "ags").symlink_to(app, target_is_directory=True)
    before = {path: path.read_bytes() for path in app.iterdir() if path.is_file()}

    seeder = CacheSeeder(tmp_path / "state")
    seeder.prune_runtime_spine_pointers(spine)
    seeder.prune_v1_skin_pointers(spine)

    after = {path: path.read_bytes() for path in app.iterdir() if path.is_file()}
    assert after == before
    assert not (app / "appearance.css").exists()
    assert not (app / "skin.css").exists()


def test_no_sleep_polling_in_the_converge_core() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "runtime"
    for relative in (
        "adapters/generation_store.py",
        "adapters/converge_lock.py",
        "adapters/converge_queue.py",
        "adapters/dbus_converge_client.py",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        assert "time.sleep" not in text, f"polling sleep in {relative}"
    assert "NameOwnerChanged" in (root / "adapters/dbus_name_watcher.py").read_text(
        encoding="utf-8"
    )
    assert "JobFinished" in (root / "adapters/dbus_converge_client.py").read_text(
        encoding="utf-8"
    )


def test_inspect_shows_desired_committed_and_applied(tmp_path: Path) -> None:
    from tests.unit.test_doctor_check import _seed_state
    from runtime.adapters.appearance_ledger import AppearanceLedger
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.application.inspect import InspectStateUseCase
    from runtime.domain.ags_apps import AppliedAppearance
    from runtime.domain.models import DesiredState

    state_root = tmp_path / "state"
    _seed_state(state_root)
    store = GenerationStore(state_root)
    committed = store.current_hash()
    assert committed is not None
    AppearanceLedger(state_root).save(
        AppliedAppearance(
            version=1,
            appearance_hash=committed,
            operation_id="run-1",
            completed_at="2026-11-06T00:00:00Z",
            apps={},
        )
    )
    desired = DesiredState(
        wallpaper="/img/new.png",
        keep=5,
        pinned=(),
        skin="glass",
        mode="light",
    )
    result = InspectStateUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
        desired=desired,
    ).run()
    assert result.desired_wallpaper == "/img/new.png"
    assert result.desired_skin == "glass"
    assert result.desired_mode == "light"
    assert result.committed_generation == committed
    assert result.applied_generation == committed


def test_gc_never_removes_an_acknowledged_generation(tmp_path: Path) -> None:
    from tests.unit.test_doctor_check import _seed_state

    state_root = tmp_path / "state"
    _seed_state(state_root)
    store = GenerationStore(state_root)
    active = store.current_hash()
    assert active is not None
    result = store.gc(
        keep=0,
        grace_seconds=0,
        acknowledged_generations=[active],
    )
    assert active not in result["generations"]
    assert store.load_manifest(active) is not None


def test_converge_engine_holds_one_lock_across_regenerate_and_commit() -> None:
    """BUG-013: regenerate + reconcile commit + history is one locked scope."""
    from types import SimpleNamespace

    from runtime.application.generation_converge import (
        ConvergeIntent,
        ConvergenceEngine,
    )

    events: list[str] = []

    class _Lock:
        def hold(self):
            from contextlib import contextmanager

            @contextmanager
            def _scope():
                events.append("lock-enter")
                try:
                    yield
                finally:
                    events.append("lock-exit")

            return _scope()

    class _Reconcile:
        def run(self, trigger: str, **kwargs: object) -> object:
            assert trigger == "reactive"
            assert kwargs["acquire_lock"] is False
            events.append("reconcile")
            return SimpleNamespace(
                generation_hash=GEN,
                reload_failures=[],
                app_refresh_results=[],
                state=object(),
            )

    class _Regenerate:
        def __init__(self) -> None:
            self.regenerated = frozenset({"palettes"})

        def __call__(self) -> object:
            events.append("regenerate")
            return self

    engine = ConvergenceEngine(
        lambda _source, _job_id: _Reconcile(),  # type: ignore[arg-type]
        lock=_Lock(),
        regenerate=_Regenerate(),
    )

    outcome = engine.converge(ConvergeIntent(trigger="reactive", source="daemon"))

    assert events == ["lock-enter", "regenerate", "reconcile", "lock-exit"]
    assert outcome.generation_hash == GEN
    assert outcome.regenerated == ("palettes",)


def test_generation_spec_requires_a_palette(tmp_path: Path) -> None:
    from tests.unit.test_doctor_check import _seed_state
    from runtime.adapters.json_state_repository import JsonStateRepository

    state_root = tmp_path / "state"
    _seed_state(state_root)
    state = JsonStateRepository(state_root=state_root).load_current()
    assert state is not None
    import dataclasses

    null_palette = dataclasses.replace(state, palette=None)
    store = GenerationStore(state_root)
    with pytest.raises(Exception, match="palette"):
        store.commit(
            GenerationSpec(
                state=null_palette,
                appearance=object(),  # type: ignore[arg-type]
                mode="dark",
                skin_name="default",
                skin_hash="a" * 64,
                intent_hash=generation_intent_hash(
                    wallpaper=state.wallpaper.content_hash,
                    palette="a" * 64,
                    skin="default",
                    mode="dark",
                ),
                monitor_names=("DP-1",),
                source_kind="cli",
            )
        )


def _content_spec(
    state_root: Path,
    *,
    intent_hash: str,
    source_kind: str = "cli",
    job_id: str | None = None,
    state: object | None = None,
) -> GenerationSpec:
    from runtime.adapters.appearance import load_appearance_entry
    from runtime.adapters.hashing import appearance_generation_hash
    from runtime.adapters.json_state_repository import JsonStateRepository

    store = GenerationStore(state_root)
    current = state or JsonStateRepository(state_root=state_root).load_current()
    assert current is not None
    assert current.palette is not None and current.skin is not None
    manifest = store.current_manifest()
    assert manifest is not None
    appearance_hash = appearance_generation_hash(
        current.palette.entry_hash,
        current.skin.entry_hash,
        manifest["mode"],
        host_hash=(
            manifest.get("host", {}).get("fingerprint")
            if isinstance(manifest.get("host"), dict)
            else None
        ),
    )
    entry = load_appearance_entry(
        state_root / "cache" / "appearance" / appearance_hash
    )
    return GenerationSpec(
        state=current,
        appearance=entry,
        mode=manifest["mode"],
        skin_name=current.skin.name,
        skin_hash=current.skin.entry_hash,
        intent_hash=intent_hash,
        monitor_names=tuple(current.monitors),
        source_kind=source_kind,  # type: ignore[arg-type]
        job_id=job_id,
    )


def test_generation_identity_ignores_operational_source_metadata(tmp_path: Path) -> None:
    from tests.unit.test_doctor_check import _seed_state

    state_root = tmp_path / "state"
    _seed_state(state_root)
    store = GenerationStore(state_root)
    before = store.current_hash()
    assert before is not None

    first = store.commit(
        _content_spec(
            state_root,
            intent_hash="a" * 64,
            source_kind="cli",
        )
    )
    second = store.commit(
        _content_spec(
            state_root,
            intent_hash="b" * 64,
            source_kind="daemon",
            job_id="operational-job",
        )
    )

    assert first.unchanged is True
    assert second.unchanged is True
    assert first.generation_hash == before
    assert second.generation_hash == before
    assert store.current_hash() == before


def test_generation_identity_changes_with_icons_content(tmp_path: Path) -> None:
    import dataclasses

    from tests.unit.test_doctor_check import _seed_state
    from runtime.adapters.json_state_repository import JsonStateRepository

    state_root = tmp_path / "state"
    _seed_state(state_root)
    store = GenerationStore(state_root)
    before = store.current_hash()
    assert before is not None
    state = JsonStateRepository(state_root=state_root).load_current()
    assert state is not None and state.icons is not None
    changed = dataclasses.replace(
        state,
        icons=dataclasses.replace(state.icons, entry_hash="f" * 64),
    )

    result = store.commit(
        _content_spec(
            state_root,
            intent_hash="a" * 64,
            state=changed,
        )
    )

    assert result.unchanged is False
    assert result.generation_hash != before
    assert store.current_hash() == result.generation_hash
    assert result.manifest["layers"]["icons"]["entryHash"] == "f" * 64
