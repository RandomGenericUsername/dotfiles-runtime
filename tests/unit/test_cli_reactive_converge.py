"""CLI composition tests for the reactive converge (P5-1-3).

Wiring-level: the composite steps are faked, but the history writer, the
backstop, and the input hasher are real, so these tests assert the AD-42
"exactly one reactive line" and the AD-36 backstop short-circuit end-to-end.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import runtime.cli.main as cli_main

TS = "2026-09-10T00:00:00Z"
WP = "e" * 64
ACTIVE = "a" * 64


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


def _h(n: int) -> str:
    return f"{n:064x}"


def _ts(n: int) -> str:
    return f"2026-09-{n:02d}T00:00:00Z"


def _write_palette(state_root: Path, entry_hash: str, ts: str) -> Path:
    d = state_root / "cache" / "palettes" / entry_hash
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "hash_algorithm": "sha256",
                "kind": "palette",
                "generated_at": ts,
                "artifact_hashes": {"colors.yaml": hashlib.sha256(b"y").hexdigest()},
            }
        ),
        encoding="utf-8",
    )
    (d / "colors.yaml").write_text("y", encoding="utf-8")
    return d


def _seed_prunable(state_root: Path) -> None:
    """Active palette + 7 old palettes: 2 removable under the keep=5 floor."""
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.domain.models import DesktopState, PaletteEntry, WallpaperEntry

    _write_palette(state_root, ACTIVE, _ts(20))
    for i in range(1, 8):
        _write_palette(state_root, _h(i), _ts(i))
    JsonStateRepository(state_root=state_root).save(
        DesktopState(
            schema_version=2,
            wallpaper=WallpaperEntry(
                hash_algorithm="sha256",
                kind="wallpaper",
                content_hash=WP,
                source_path="/img/w.png",
                imported_at=_ts(20),
            ),
            monitors={},
            palette=PaletteEntry(
                hash_algorithm="sha256",
                kind="palette",
                entry_hash=ACTIVE,
                source_wallpaper_hash=WP,
                input_template_hash="t" * 64,
                artifact_hashes={},
                generated_at=_ts(20),
            ),
            effects=None,
            icons=None,
            applied_at=_ts(20),
        )
    )


def _write_desired(*, wallpaper: str = "/img/w.png", keep: int = 5) -> None:
    intent = cli_main._resolve_desired_path()
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.write_text(
        json.dumps({"version": 1, "wallpaper": wallpaper, "keep": keep, "pinned": []}),
        encoding="utf-8",
    )


def _palette_entries(state_root: Path) -> set[str]:
    layer = state_root / "cache" / "palettes"
    if not layer.exists():
        return set()
    return {p.name for p in layer.iterdir() if p.is_dir()}


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

    result = cli_main._run_reactive_converge(
        observe_only=False, prune_on_reactive=True
    )

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

    cli_main._run_reactive_converge(observe_only=False, prune_on_reactive=True)
    result = cli_main._run_reactive_converge(
        observe_only=False, prune_on_reactive=True
    )

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

    result = cli_main._run_reactive_converge(
        observe_only=False, prune_on_reactive=True
    )

    assert result.ran is True  # the other steps still converge
    assert calls == ["check", "regenerate", "reconcile", "prune"]
    assert len(_history_lines(state_root)) == 1


class TestReactivePrunePolicy:
    """Reactive prune is opt-in (default off): no deletion unless asked (AD-30)."""

    def test_default_off_removes_nothing_and_writes_no_prune_line(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        state_root = cli_main._resolve_state_root()
        state_root.mkdir(parents=True, exist_ok=True)
        _seed_prunable(state_root)
        _write_desired()
        before = _palette_entries(state_root)
        calls: list[str] = []
        # NOTE: neither _run_converge NOR _run_prune is faked — the real
        # declarative step runs (with allow_delete=False) and the real
        # read-only skip log computes the would-be count. This is the R1
        # regression: with the opt-in off nothing may be deleted.
        monkeypatch.setattr(cli_main, "_run_check_inputs", lambda: calls.append("check"))
        monkeypatch.setattr(
            cli_main, "_run_regenerate_stale", lambda **_: calls.append("regenerate")
        )
        monkeypatch.setattr(cli_main, "_run_reconcile", lambda **_: calls.append("reconcile"))

        with caplog.at_level("INFO", logger="runtime.cli.main"):
            result = cli_main._run_reactive_converge(observe_only=False)

        assert result.ran is True
        assert calls == ["check", "regenerate", "reconcile"]
        lines = _history_lines(state_root)
        assert len(lines) == 1
        assert lines[0]["trigger"] == "reactive"
        assert not any(line["trigger"] == "prune" for line in lines)
        assert _palette_entries(state_root) == before  # zero deletions
        assert any("reactive: prune skipped (opt-in off" in r.message for r in caplog.records)
        # The would-be count is reported (3 old palettes beyond the keep=5 floor).
        assert any("3 entries would be removed" in r.message for r in caplog.records)

    def test_opt_in_performs_real_prune_and_respects_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_root = cli_main._resolve_state_root()
        state_root.mkdir(parents=True, exist_ok=True)
        _seed_prunable(state_root)
        _write_desired()
        before = _palette_entries(state_root)
        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_check_inputs", lambda: calls.append("check"))
        monkeypatch.setattr(
            cli_main, "_run_regenerate_stale", lambda **_: calls.append("regenerate")
        )
        monkeypatch.setattr(cli_main, "_run_reconcile", lambda **_: calls.append("reconcile"))

        result = cli_main._run_reactive_converge(observe_only=False, prune_on_reactive=True)

        assert result.ran is True
        assert calls == ["check", "regenerate", "reconcile"]
        lines = _history_lines(state_root)
        reactive = [line for line in lines if line["trigger"] == "reactive"]
        prune = [line for line in lines if line["trigger"] == "prune"]
        assert len(reactive) == 1
        assert len(prune) == 1
        after = _palette_entries(state_root)
        removed_actual = len(before) - len(after)
        # The opt-in path deletes for real and the audit line matches exactly.
        assert removed_actual == 3
        details = prune[0]["details"]
        assert details["removed"] == removed_actual
        assert details["removed"] > 0  # not the old misleading removed=0
        assert details["layers"]["palettes"] == removed_actual
        # AD-30 floor: active (newest) + the next four dated survive; days 1-3 go.
        assert after == {ACTIVE, *(_h(i) for i in range(4, 8))}

    def test_skip_log_reports_zero_when_nothing_removable(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        state_root = _seed_state()  # no cache entries
        calls: list[str] = []
        _fake_composite(monkeypatch, calls)

        with caplog.at_level("INFO", logger="runtime.cli.main"):
            result = cli_main._run_reactive_converge(observe_only=False)

        assert result.ran is True
        assert any(
            "0 entries would be removed" in r.message for r in caplog.records
        )


def test_unseeded_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _fake_composite(monkeypatch, calls)

    result = cli_main._run_reactive_converge(observe_only=False)

    assert result.ran is False
    assert result.reason == "unseeded"
    assert calls == []


def test_daemon_converge_excludes_terminal_reloader(monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon call site passes ``include_terminal=False`` to every
    reloader-bearing step (no controlling tty → no /dev/tty failure noise),
    while the CLI keeps the default True."""
    _seed_state()
    captured: dict[str, dict[str, object]] = {}

    def _record(step: str) -> object:
        def _inner(**kwargs: object) -> object:
            captured[step] = kwargs
            return None

        return _inner

    monkeypatch.setattr(cli_main, "_run_check_inputs", lambda: None)
    monkeypatch.setattr(cli_main, "_run_regenerate_stale", _record("regenerate"))
    monkeypatch.setattr(cli_main, "_run_reconcile", _record("reconcile"))
    monkeypatch.setattr(cli_main, "_run_converge", _record("declarative"))

    cli_main._run_reactive_converge(observe_only=False)

    assert captured["regenerate"]["include_terminal"] is False
    assert captured["reconcile"]["include_terminal"] is False
    assert captured["declarative"]["include_terminal"] is False


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
