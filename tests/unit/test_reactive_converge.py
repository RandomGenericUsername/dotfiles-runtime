"""Reactive converge decision/ordering tests (AD-35/AD-36/AD-42).

Pure fakes: no use case, no lock, no disk. The "exactly one reactive line"
property is enforced by asserting the append callable fires exactly once and
that every composite step runs before it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.adapters.converge_backstop import LastConvergedBackstop
from runtime.adapters.seeder import CacheSeeder
from runtime.application.converge import ReactiveConvergeUseCase


class _Harness:
    def __init__(
        self,
        *,
        current_hash: str = "hash-1",
        backstop: str | None = None,
        has_state: bool = True,
        observe_only: bool = False,
        fail_step: str | None = None,
    ) -> None:
        self.log: list[str] = []
        self.written: list[str] = []
        self.append_calls = 0
        self._current_hash = current_hash
        self._backstop = backstop
        self._has_state = has_state
        self._observe_only = observe_only
        self._fail_step = fail_step

        def _step(name: str):
            def _run() -> object:
                if fail_step == name:
                    raise RuntimeError(f"{name} failed")
                self.log.append(name)
                return object()

            return _run

        def _append() -> None:
            if fail_step == "append":
                raise RuntimeError("append failed")
            self.append_calls += 1
            self.log.append("append")

        self.use_case = ReactiveConvergeUseCase(
            input_hash=lambda: self._current_hash,
            read_backstop=lambda: self._backstop,
            write_backstop=self.written.append,
            has_state=lambda: self._has_state,
            check_inputs=_step("check"),
            regenerate_stale=_step("regenerate"),
            reconcile=_step("reconcile"),
            declarative=_step("declarative"),
            append_history=_append,
            prune=_step("prune"),
            observe_only=self._observe_only,
        )

    def run(self):
        return self.use_case.run()


def test_unchanged_backstop_is_noop() -> None:
    harness = _Harness(current_hash="same", backstop="same")
    result = harness.run()
    assert result.ran is False
    assert result.reason == "unchanged"
    assert harness.log == []
    assert harness.append_calls == 0
    assert harness.written == []


def test_absent_backstop_converges() -> None:
    harness = _Harness(current_hash="new", backstop=None)
    result = harness.run()
    assert result.ran is True
    assert result.reason == "converged"
    assert harness.written == ["new"]


def test_unseeded_is_benign_noop() -> None:
    harness = _Harness(current_hash="new", backstop="old", has_state=False)
    result = harness.run()
    assert result.ran is False
    assert result.reason == "unseeded"
    assert harness.log == []
    assert harness.append_calls == 0
    assert harness.written == []  # never persists a backstop without a converge


def test_observe_only_appends_no_history() -> None:
    harness = _Harness(current_hash="new", backstop="old", observe_only=True)
    result = harness.run()
    assert result.ran is False
    assert result.reason == "observe-only"
    assert harness.log == []
    assert harness.append_calls == 0
    assert harness.written == []


def test_active_composite_order_and_single_audit_line() -> None:
    harness = _Harness(current_hash="new", backstop="old", observe_only=False)
    result = harness.run()
    assert result.ran is True
    assert harness.log == [
        "check",
        "regenerate",
        "reconcile",
        "declarative",
        "prune",
        "append",
    ]
    assert harness.append_calls == 1
    assert harness.written == ["new"]


def test_composite_failure_leaves_backstop_unwritten() -> None:
    harness = _Harness(current_hash="new", backstop="old", fail_step="reconcile")
    with pytest.raises(RuntimeError, match="reconcile failed"):
        harness.run()
    assert harness.append_calls == 0
    assert harness.written == []


def test_append_failure_leaves_backstop_unwritten() -> None:
    harness = _Harness(current_hash="new", backstop="old", fail_step="append")
    with pytest.raises(RuntimeError, match="append failed"):
        harness.run()
    assert harness.written == []


def test_prune_optional() -> None:
    harness = _Harness(current_hash="new", backstop="old")
    harness.use_case._prune = None  # type: ignore[attr-defined]
    result = harness.run()
    assert result.ran is True
    assert "prune" not in harness.log


def test_seeder_suppresses_history(tmp_path: Path) -> None:
    """The composite's inner seeders must write nothing (single reactive line)."""
    suppressed = CacheSeeder(tmp_path, suppress_history=True)
    suppressed.append_history(trigger="reactive", wallpaper_hash="ab" * 32)
    assert not (tmp_path / "history.jsonl").exists()

    CacheSeeder(tmp_path).append_history(trigger="reactive", wallpaper_hash="ab" * 32)
    lines = (tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_backstop_round_trip_through_use_case(tmp_path: Path) -> None:
    backstop = LastConvergedBackstop(tmp_path)
    first = ReactiveConvergeUseCase(
        input_hash=lambda: "h1",
        read_backstop=backstop.read,
        write_backstop=backstop.write,
        has_state=lambda: True,
        check_inputs=lambda: None,
        regenerate_stale=lambda: None,
        reconcile=lambda: None,
        declarative=lambda: None,
        append_history=lambda: None,
        prune=None,
        observe_only=False,
    ).run()
    assert first.ran is True
    # Second run sees the persisted identical hash: no-op (idempotent).
    second = ReactiveConvergeUseCase(
        input_hash=lambda: "h1",
        read_backstop=backstop.read,
        write_backstop=backstop.write,
        has_state=lambda: True,
        check_inputs=lambda: None,
        regenerate_stale=lambda: None,
        reconcile=lambda: None,
        declarative=lambda: None,
        append_history=lambda: None,
        prune=None,
        observe_only=False,
    ).run()
    assert second.ran is False
    assert second.reason == "unchanged"


def test_no_lock_is_held_by_the_use_case() -> None:
    """Static guard (AD-35): the composite imports no mutex/lock machinery.

    The daemon must not hold a lock across a use-case call; the composite
    itself never acquires one (use cases own their locks internally).
    """
    import runtime.application.converge as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "ISeedMutex" not in source
    assert "Flock" not in source
    assert ".hold(" not in source
