"""CLI wiring tests for the declarative planner (Phase 4, Story 4.5).

Covers: --plan declarative rendering (AC 1), legacy path untouched (AC 2,
via the existing plan suite + branch selection here), execute hook
rendering (AC 3), malformed desired.json failing loud (AC 4).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.application.planner import ConvergenceReport
from runtime.cli.main import _DeclarativePlanResult, app
from runtime.domain.models import ChangeSet

runner = CliRunner()

PIN_A = "aa" * 32
PIN_B = "bb" * 32


@pytest.fixture(autouse=True)
def _quiet_seed_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point first-run seeding at an empty spine so it skips quietly."""
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    # Phase 5: the intent document is relocated under $XDG_CONFIG_HOME.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    # The reconcile command publishes wallpaper.state; keep tests off the bus.
    monkeypatch.setattr(
        cli_main, "_build_wallpaper_client", lambda: SimpleNamespace(publish=lambda *a, **k: None)
    )


def _intent_path(tmp_path: Path) -> Path:
    return tmp_path / "config-home" / "dotfiles" / "desired.json"


def _declarative_result() -> _DeclarativePlanResult:
    return _DeclarativePlanResult(
        stale=frozenset(),
        fresh=frozenset({"palettes"}),
        changeset=ChangeSet(
            wallpaper_target="/img/wall.png",
            pins_to_add=(PIN_A,),
            pins_absent=(PIN_B,),
            keep_target=3,
        ),
        prunable={"wallpapers": ("01" * 32,), "palettes": (), "effects": (), "icons": ()},
        total_removable=1,
        compute_keep=3,
        current_keep=5,
        prune_pinned=False,
    )


def _reconcile_result() -> SimpleNamespace:
    return SimpleNamespace(
        repointed=[Path("/state/current/wallpaper-DP-1.png")],
        skipped=[],
        consumer_symlinks=[],
        cache_regenerated=[],
        reload_failures=[],
        state=SimpleNamespace(wallpaper=SimpleNamespace(content_hash="ff" * 32)),
    )


def test_plan_renders_declarative_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "runtime.cli.main._run_reconcile_plan",
        lambda keep=None, prune_pinned=False: _declarative_result(),
    )
    result = runner.invoke(app, ["reconcile", "--plan"])
    assert result.exit_code == 0, result.output
    assert "declarative gap vs desired.json:" in result.output
    assert "wallpaper -> /img/wall.png" in result.output
    assert "pins to add (1):" in result.output
    assert "pins absent (protected, informational) (1):" in result.output
    assert "keep: 5 -> 3" in result.output
    assert "prunable under AD-30 floor (keep=3): 1" in result.output


def test_plan_renders_converged_state(monkeypatch: pytest.MonkeyPatch) -> None:
    converged = _DeclarativePlanResult(
        stale=frozenset(),
        fresh=frozenset({"palettes"}),
        changeset=ChangeSet(
            wallpaper_target=None, pins_to_add=(), pins_absent=(), keep_target=None
        ),
        prunable={"wallpapers": (), "palettes": (), "effects": (), "icons": ()},
        total_removable=0,
        compute_keep=5,
        current_keep=5,
        prune_pinned=False,
    )
    monkeypatch.setattr(
        "runtime.cli.main._run_reconcile_plan", lambda keep=None, prune_pinned=False: converged
    )
    result = runner.invoke(app, ["reconcile", "--plan"])
    assert result.exit_code == 0, result.output
    assert "already converged: desired state matches actual" in result.output


def test_plan_malformed_desired_fails_loud(tmp_path: Path) -> None:
    intent = _intent_path(tmp_path)
    intent.parent.mkdir(parents=True)
    intent.write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["reconcile", "--plan"])
    assert result.exit_code == 1
    assert "desired.json" in (result.output + getattr(result, "stderr", ""))


def test_execute_hook_renders_converge_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runtime.cli.main._run_reconcile", lambda: _reconcile_result())
    report = ConvergenceReport(
        wallpaper_set="/img/wall.png",
        deleted={"wallpapers": ("01" * 32,)},
        pins_pending=(PIN_A,),
        keep_target=3,
    )
    monkeypatch.setattr("runtime.cli.main._run_converge", lambda: report)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert "converged wallpaper: /img/wall.png" in result.output
    assert "pruned under AD-30 floor: 1" in result.output
    assert "pins pending (manual): 1" in result.output
    assert "keep policy: 3 (in force)" in result.output


def test_execute_hook_absent_desired_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runtime.cli.main._run_reconcile", lambda: _reconcile_result())
    monkeypatch.setattr("runtime.cli.main._run_converge", lambda: None)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert "converged wallpaper" not in result.output
    assert "pruned under AD-30 floor" not in result.output


def test_run_converge_returns_none_without_desired() -> None:
    assert cli_main._run_converge() is None


def test_run_converge_malformed_desired_raises(tmp_path: Path) -> None:
    intent = _intent_path(tmp_path)
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.write_text('{"version": 9}', encoding="utf-8")
    with pytest.raises(ValueError, match="desired state version"):
        cli_main._run_converge()


def test_run_converge_reload_failure_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reload failures are fatal, never reported as success (Item 2)."""
    import json

    intent = _intent_path(tmp_path)
    intent.parent.mkdir(parents=True)
    intent.write_text(
        json.dumps(
            {
                "version": 1,
                "wallpaper": "/img/new.png",
                "keep": 5,
                "pinned": [],
            }
        ),
        encoding="utf-8",
    )
    state_root = tmp_path / "state-home" / "dotfiles"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "wallpaper": {
                    "hash": "dd" * 32,
                    "source_path": "/img/old.png",
                    "applied_at": "2026-09-10T00:00:00Z",
                },
                "monitors": {},
                "palette": None,
                "effects": None,
                "icons": None,
                "applied_at": "2026-09-10T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_main,
        "_run_wallpaper_set",
        lambda target, **_: SimpleNamespace(
            reconcile=SimpleNamespace(reload_failures=["Hyprpaper"])
        ),
    )
    with pytest.raises(RuntimeError, match="reload failed for: Hyprpaper"):
        cli_main._run_converge()


def test_imperative_render_golden() -> None:
    """Golden test (AC 2): legacy render locked byte-for-byte."""
    from runtime.cli.main import _ReconcilePlanResult, _render_imperative_plan

    seen: dict[str, object] = {}

    class _Renderer:
        def custom(self, view: object) -> None:
            seen["view"] = view

        def error(self, view: object) -> None:
            raise AssertionError("no error expected")

    legacy = _ReconcilePlanResult(
        stale=frozenset({"palettes"}),
        fresh=frozenset({"effects", "icons"}),
        removals={
            "wallpapers": (),
            "palettes": ("aa" * 32,),
            "effects": (),
            "icons": (),
        },
        kept={"wallpapers": 1, "palettes": 5, "effects": 0, "icons": 0},
        total_removable=1,
        keep=5,
        prune_pinned=False,
    )
    _render_imperative_plan(_Renderer(), legacy)
    view = seen["view"]
    assert view.plain == (
        "stale layers: palettes (fresh: effects, icons)\n"
        "reclaimable: 1\n"
        "palettes (1):\n"
        f"  {('aa' * 32)[:12]}"
    )
    assert view.object == {
        "stale": ["palettes"],
        "fresh": ["effects", "icons"],
        "removals": {
            "wallpapers": [],
            "palettes": ["aa" * 32],
            "effects": [],
            "icons": [],
        },
        "kept": {"wallpapers": 1, "palettes": 5, "effects": 0, "icons": 0},
        "total_removable": 1,
        "keep": 5,
        "prune_pinned": False,
    }


def test_floor_fixture_active_undated_survive_zero_keep() -> None:
    """AD-30 floor (AC 3): keep=0 + prune_pinned still spares active+undated."""
    from runtime.application.actual_state import build_actual_state
    from runtime.domain.models import (
        CacheEntryRef,
        DesktopState,
        PaletteEntry,
        WallpaperEntry,
    )

    wh, active_extra, pinned, undated = "dd" * 32, "01" * 32, "02" * 32, "03" * 32
    refs = {
        "wallpapers": [
            CacheEntryRef(wh, "2026-09-10T00:00:00Z"),
            CacheEntryRef(active_extra, "2026-09-01T00:00:00Z"),
            CacheEntryRef(undated, None),
        ],
        "palettes": [
            CacheEntryRef("aa" * 32, "2026-09-10T00:00:00Z"),
            CacheEntryRef(pinned, "2026-09-02T00:00:00Z"),
        ],
        "effects": [],
        "icons": [],
    }
    pins = {"wallpapers": set(), "palettes": {pinned}, "effects": set(), "icons": set()}
    current = DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wh,
            source_path="/img/wall.png",
            imported_at="2026-09-10T00:00:00Z",
        ),
        monitors={},
        palette=PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash="aa" * 32,
            source_wallpaper_hash=wh,
            input_template_hash="t" * 64,
            artifact_hashes={},
            generated_at="2026-09-10T00:00:00Z",
        ),
        effects=None,
        icons=None,
        applied_at="2026-09-10T00:00:00Z",
    )
    actual = build_actual_state(
        current, lambda layer: refs[layer], lambda: pins, keep=0, prune_pinned=True
    )
    wall_prunable = actual.prunable_hashes["wallpapers"]
    assert wh not in wall_prunable  # active survives
    assert undated not in wall_prunable  # undated survives
    assert active_extra in wall_prunable  # old dated goes
    assert pinned in actual.prunable_hashes["palettes"]  # pins prunable when asked


def test_plain_reconcile_malformed_desired_fails_before_swap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    intent = _intent_path(tmp_path)
    intent.parent.mkdir(parents=True)
    intent.write_text('{"version": 9}', encoding="utf-8")
    calls: list[None] = []

    def _swap() -> object:
        calls.append(None)
        raise AssertionError("_run_reconcile must not run on malformed intent")

    monkeypatch.setattr(cli_main, "_run_reconcile", _swap)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 1
    assert calls == []
    output = result.output + getattr(result, "stderr", "")
    assert "desired.json" in output
    assert "version" in output


def _write_palette_entry(state_root: Path, entry_hash: str, generated_at: str) -> None:
    import hashlib
    import json

    d = state_root / "cache" / "palettes" / entry_hash
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "hash_algorithm": "sha256",
                "kind": "palette",
                "generated_at": generated_at,
                "artifact_hashes": {"colors.yaml": hashlib.sha256(b"y").hexdigest()},
            }
        ),
        encoding="utf-8",
    )
    (d / "colors.yaml").write_text("y", encoding="utf-8")


def _seed_prunable_palettes(state_root: Path) -> None:
    """Active palette + 7 older dated entries; desired matches the active one."""
    import json

    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.domain.models import DesktopState, PaletteEntry, WallpaperEntry

    active = "a" * 64
    wallpaper = "e" * 64
    _write_palette_entry(state_root, active, "2026-09-20T00:00:00Z")
    for i in range(1, 8):
        _write_palette_entry(state_root, f"{i:064x}", f"2026-09-{i:02d}T00:00:00Z")
    JsonStateRepository(state_root=state_root).save(
        DesktopState(
            schema_version=2,
            wallpaper=WallpaperEntry(
                hash_algorithm="sha256",
                kind="wallpaper",
                content_hash=wallpaper,
                source_path="/img/w.png",
                imported_at="2026-09-20T00:00:00Z",
            ),
            monitors={},
            palette=PaletteEntry(
                hash_algorithm="sha256",
                kind="palette",
                entry_hash=active,
                source_wallpaper_hash=wallpaper,
                input_template_hash="t" * 64,
                artifact_hashes={},
                generated_at="2026-09-20T00:00:00Z",
            ),
            effects=None,
            icons=None,
            applied_at="2026-09-20T00:00:00Z",
        )
    )
    intent = cli_main._resolve_desired_path()
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.write_text(
        json.dumps({"version": 1, "wallpaper": "/img/w.png", "keep": 5, "pinned": []}),
        encoding="utf-8",
    )


def _palette_dir_entries(state_root: Path) -> set[str]:
    layer = state_root / "cache" / "palettes"
    if not layer.is_dir():
        return set()
    return {p.name for p in layer.iterdir() if p.is_dir()}


def test_run_converge_defaults_to_real_deletion_for_manual_reconcile() -> None:
    """Phase 4 behavior unchanged: plain ``reconcile`` deletes beyond-keep."""
    state_root = cli_main._resolve_state_root()
    state_root.mkdir(parents=True, exist_ok=True)
    _seed_prunable_palettes(state_root)

    report = cli_main._run_converge()

    assert report is not None
    assert sum(len(hashes) for hashes in report.deleted.values()) == 3
    assert len(_palette_dir_entries(state_root)) == 5


def test_run_converge_allow_delete_false_never_deletes() -> None:
    """R1: the reactive declarative leg plans but physically removes nothing."""
    state_root = cli_main._resolve_state_root()
    state_root.mkdir(parents=True, exist_ok=True)
    _seed_prunable_palettes(state_root)
    before = _palette_dir_entries(state_root)

    report = cli_main._run_converge(allow_delete=False)

    assert report is not None
    assert sum(len(hashes) for hashes in report.deleted.values()) == 0
    assert _palette_dir_entries(state_root) == before
