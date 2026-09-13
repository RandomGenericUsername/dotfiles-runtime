"""CLI composition tests for the reactive converge (P5-1-3).

Wiring-level: the composite steps are faked, but the history writer, the
backstop, and the input hasher are real, so these tests assert the AD-42
"exactly one reactive line" and the AD-36 backstop short-circuit end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import runtime.cli.main as cli_main

TS = "2026-09-10T00:00:00Z"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))


def _seed_state() -> Path:
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.domain.models import DesktopState, WallpaperEntry

    state_root = cli_main._resolve_state_root()
    JsonStateRepository(state_root=state_root).save(
        DesktopState(
            schema_version=2,
            wallpaper=WallpaperEntry(
                hash_algorithm="sha256",
                kind="wallpaper",
                content_hash="ab" * 32,
                source_path="/img/w.png",
                imported_at=TS,
            ),
            monitors={},
            palette=None,
            effects=None,
            icons=None,
            applied_at=TS,
        )
    )
    return state_root


def _history_lines(state_root: Path) -> list[dict[str, object]]:
    path = state_root / "history.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _fake_composite(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    monkeypatch.setattr(cli_main, "_run_check_inputs", lambda: calls.append("check"))
    monkeypatch.setattr(
        cli_main, "_run_regenerate_stale", lambda **_: calls.append("regenerate")
    )
    monkeypatch.setattr(cli_main, "_run_reconcile", lambda **_: calls.append("reconcile"))
    monkeypatch.setattr(cli_main, "_run_converge", lambda **_: calls.append("declarative"))
    monkeypatch.setattr(cli_main, "_run_prune", lambda **_: calls.append("prune"))


def test_active_converge_writes_one_reactive_line_and_backstop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = _seed_state()
    calls: list[str] = []
    _fake_composite(monkeypatch, calls)

    result = cli_main._run_reactive_converge(observe_only=False)

    assert result.ran is True
    assert result.reason == "converged"
    assert calls == ["check", "regenerate", "reconcile", "declarative", "prune"]
    lines = _history_lines(state_root)
    assert len(lines) == 1
    assert lines[0]["trigger"] == "reactive"
    assert (state_root / "last-converged.json").is_file()


def test_second_converge_is_a_backstop_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    state_root = _seed_state()
    calls: list[str] = []
    _fake_composite(monkeypatch, calls)

    cli_main._run_reactive_converge(observe_only=False)
    result = cli_main._run_reactive_converge(observe_only=False)

    assert result.ran is False
    assert result.reason == "unchanged"
    assert calls == ["check", "regenerate", "reconcile", "declarative", "prune"]
    assert len(_history_lines(state_root)) == 1


def test_observe_only_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    state_root = _seed_state()
    calls: list[str] = []
    _fake_composite(monkeypatch, calls)

    result = cli_main._run_reactive_converge(observe_only=True)

    assert result.ran is False
    assert result.reason == "observe-only"
    assert calls == []
    assert _history_lines(state_root) == []
    assert not (state_root / "last-converged.json").exists()


def test_corrupt_intent_is_recoverable_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    state_root = _seed_state()
    intent = cli_main._resolve_desired_path()
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.write_text("{not json", encoding="utf-8")
    calls: list[str] = []
    _fake_composite(monkeypatch, calls)

    def _boom(**_kwargs: object) -> object:
        raise ValueError("desired state is not valid JSON")

    monkeypatch.setattr(cli_main, "_run_converge", _boom)

    result = cli_main._run_reactive_converge(observe_only=False)

    assert result.ran is True  # the other steps still converge
    assert calls == ["check", "regenerate", "reconcile", "prune"]
    assert len(_history_lines(state_root)) == 1


def test_unseeded_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _fake_composite(monkeypatch, calls)

    result = cli_main._run_reactive_converge(observe_only=False)

    assert result.ran is False
    assert result.reason == "unseeded"
    assert calls == []


def test_backstop_lives_under_state_root_and_is_not_watched() -> None:
    from runtime.adapters.converge_backstop import BACKSTOP_FILENAME, LastConvergedBackstop
    from runtime.adapters.watch_roots import enumerate_watch_roots

    state_root = cli_main._resolve_state_root()
    assert LastConvergedBackstop(state_root).path == state_root / BACKSTOP_FILENAME
    watched = {
        str(root.path)
        for root in enumerate_watch_roots(
            cli_main._resolve_install_spine(), cli_main._resolve_desired_path()
        )
    }
    assert str(state_root / BACKSTOP_FILENAME) not in watched
    assert not any(str(state_root) in path for path in watched)
