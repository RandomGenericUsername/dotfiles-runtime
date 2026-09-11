"""Unit tests for the ``reconcile --plan`` read-only preview (Story 4.1).

Covers: CLI shape (exit codes, plain/json rendering, mutual exclusion,
absent-state exit 1, seed guard), and a real end-to-end (tmp state + spine,
template edit -> exact stale set + removal plan) with an FS-snapshot
zero-mutation pin. Mirrors test_cli_check_inputs.py patterns — monkeypatch
the composition helper for shape tests, never the whole app.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.cli.main import _ReconcilePlanResult, app

runner = CliRunner()

PH = "aa" * 32
EH = "bb" * 32
IH = "cc" * 32
WH = "dd" * 32


@pytest.fixture(autouse=True)
def _quiet_seed_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point first-run seeding at an empty spine so it skips quietly."""
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _plan_result(
    stale: set[str] | frozenset[str] = frozenset(),
    fresh: set[str] | frozenset[str] = frozenset({"palettes", "effects", "icons"}),
    removals: dict[str, tuple[str, ...]] | None = None,
    keep: int = 5,
    prune_pinned: bool = False,
) -> _ReconcilePlanResult:
    removals = (
        removals
        if removals is not None
        else {layer: () for layer in ("wallpapers", "palettes", "effects", "icons")}
    )
    kept = {layer: 0 for layer in ("wallpapers", "palettes", "effects", "icons")}
    total = sum(len(hashes) for hashes in removals.values())
    return _ReconcilePlanResult(
        stale=frozenset(stale),
        fresh=frozenset(fresh),
        removals=removals,
        kept=kept,
        total_removable=total,
        keep=keep,
        prune_pinned=prune_pinned,
    )


def _fake_composition(
    monkeypatch: pytest.MonkeyPatch,
    result: _ReconcilePlanResult,
    seen: dict[str, object] | None = None,
) -> None:
    def _run(keep: int = 5, prune_pinned: bool = False) -> _ReconcilePlanResult:
        if seen is not None:
            seen.update(keep=keep, prune_pinned=prune_pinned)
        return result

    monkeypatch.setattr("runtime.cli.main._run_reconcile_plan", _run)


class TestReconcilePlanCliShape:
    def test_clean_reports_converged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_composition(monkeypatch, _plan_result())
        result = runner.invoke(app, ["reconcile", "--plan"])
        assert result.exit_code == 0
        assert "all layers fresh" in result.output
        assert "nothing reclaimable" in result.output

    def test_stale_and_removals_named_in_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stale_hash = "ab" * 32
        _fake_composition(
            monkeypatch,
            _plan_result(
                stale={"palettes", "icons"},
                fresh={"effects"},
                removals={
                    "wallpapers": (),
                    "palettes": (stale_hash,),
                    "effects": (),
                    "icons": (),
                },
            ),
        )
        result = runner.invoke(app, ["reconcile", "--plan"])
        assert result.exit_code == 0
        assert "palettes" in result.output
        assert "reclaimable: 1" in result.output
        assert stale_hash[:12] in result.output

    def test_json_object_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stale_hash = "ab" * 32
        seen: dict[str, object] = {}
        _fake_composition(
            monkeypatch,
            _plan_result(
                stale={"palettes", "icons"},
                fresh={"effects"},
                removals={
                    "wallpapers": (),
                    "palettes": (stale_hash,),
                    "effects": (),
                    "icons": (),
                },
                keep=3,
                prune_pinned=True,
            ),
            seen,
        )
        result = runner.invoke(
            app, ["reconcile", "--plan", "--keep", "3", "--prune-pinned", "--format", "json"]
        )
        assert result.exit_code == 0
        assert seen == {"keep": 3, "prune_pinned": True}
        payload = json.loads(result.output)
        assert payload["stale"] == ["icons", "palettes"]
        assert payload["fresh"] == ["effects"]
        assert payload["removals"]["palettes"] == [stale_hash]
        assert payload["total_removable"] == 1
        assert payload["keep"] == 3
        assert payload["prune_pinned"] is True

    def test_absent_state_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(keep: int = 5, prune_pinned: bool = False) -> _ReconcilePlanResult:
            raise ValueError("no runtime state recorded yet")

        monkeypatch.setattr("runtime.cli.main._run_reconcile_plan", _raise)
        result = runner.invoke(app, ["reconcile", "--plan"])
        assert result.exit_code == 1

    def test_mutual_exclusion_plan_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = runner.invoke(app, ["reconcile", "--plan", "--check-inputs"])
        assert result.exit_code == 2
        assert "MutuallyExclusiveOptions" in (result.output + getattr(result, "stderr", ""))

    def test_mutual_exclusion_plan_regenerate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = runner.invoke(app, ["reconcile", "--plan", "--regenerate-stale"])
        assert result.exit_code == 2
        assert "MutuallyExclusiveOptions" in (result.output + getattr(result, "stderr", ""))

    def test_mutual_exclusion_all_three(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = runner.invoke(app, ["reconcile", "--plan", "--check-inputs", "--regenerate-stale"])
        assert result.exit_code == 2
        assert "MutuallyExclusiveOptions" in (result.output + getattr(result, "stderr", ""))

    def test_mutual_exclusion_check_regenerate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = runner.invoke(app, ["reconcile", "--check-inputs", "--regenerate-stale"])
        assert result.exit_code == 2
        assert "MutuallyExclusiveOptions" in (result.output + getattr(result, "stderr", ""))

    def test_orphaned_keep_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan_calls: list[None] = []

        def _nope(keep: int = 5, prune_pinned: bool = False) -> _ReconcilePlanResult:
            plan_calls.append(None)
            raise AssertionError("composition must not run when flags are orphaned")

        monkeypatch.setattr(cli_main, "_run_reconcile_plan", _nope)
        result = runner.invoke(app, ["reconcile", "--keep", "1"])
        assert result.exit_code == 2
        assert "MutuallyExclusiveOptions" in (result.output + getattr(result, "stderr", ""))
        assert plan_calls == []

    def test_orphaned_prune_pinned_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan_calls: list[None] = []

        def _nope(keep: int = 5, prune_pinned: bool = False) -> _ReconcilePlanResult:
            plan_calls.append(None)
            raise AssertionError("composition must not run when flags are orphaned")

        monkeypatch.setattr(cli_main, "_run_reconcile_plan", _nope)
        result = runner.invoke(app, ["reconcile", "--check-inputs", "--prune-pinned"])
        assert result.exit_code == 2
        assert "MutuallyExclusiveOptions" in (result.output + getattr(result, "stderr", ""))
        assert plan_calls == []

    def test_negative_keep_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan_calls: list[None] = []

        def _nope(keep: int = 5, prune_pinned: bool = False) -> _ReconcilePlanResult:
            plan_calls.append(None)
            raise AssertionError("composition must not run on usage error")

        monkeypatch.setattr(cli_main, "_run_reconcile_plan", _nope)
        result = runner.invoke(app, ["reconcile", "--plan", "--keep", "-1"])
        assert result.exit_code == 2
        assert plan_calls == []

    def test_plan_never_seeds(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Seed guard covers --plan even when a seedable spine exists."""
        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: calls.append("seed"))
        install = tmp_path / "install"
        (install / "wallpapers").mkdir(parents=True)
        (install / "wallpapers" / "default.png").write_bytes(b"png")
        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(install))
        cli_main.main_callback(ctx=SimpleNamespace(invoked_subcommand="reconcile"))
        assert calls == []


def _snapshot(root: Path) -> dict[str, str]:
    """Map every path under root to a state tag (FS-mutation pin)."""
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        try:
            st = path.stat(follow_symlinks=False)
        except OSError:
            snapshot[rel] = "vanished"
            continue
        tag = f"{st.st_mode:o}:{st.st_mtime_ns}"
        if path.is_symlink():
            snapshot[rel] = f"link->{os.readlink(path)}:{tag}"
        elif path.is_file():
            snapshot[rel] = f"file:{hashlib.sha256(path.read_bytes()).hexdigest()}:{tag}"
        elif path.is_dir():
            snapshot[rel + "/"] = f"dir:{tag}"
    return snapshot


def _build_spine(install: Path) -> dict[str, Path]:
    templates = install / "config" / "color-scheme-generator" / "templates"
    templates.mkdir(parents=True)
    (templates / "a.j2").write_text("template-a", encoding="utf-8")
    catalog = install / "config" / "weg" / "effects.yaml"
    catalog.parent.mkdir(parents=True)
    catalog.write_text("effects: []", encoding="utf-8")
    icon_templates = install / "icon-templates"
    icon_templates.mkdir(parents=True)
    (icon_templates / "icon.svg").write_text("<svg/>", encoding="utf-8")
    icon_mappings = install / "icon-mappings" / "icons.yaml"
    icon_mappings.parent.mkdir(parents=True)
    icon_mappings.write_text("mappings: {}", encoding="utf-8")
    return {"templates": templates}


class TestReconcilePlanEndToEnd:
    """Real wiring: tmp state + spine, no monkeypatched composition."""

    def _seed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> tuple[Path, Path, dict[str, Path]]:
        from runtime.adapters.hashing import canonical_hash_dir, hash_file
        from runtime.adapters.json_state_repository import JsonStateRepository
        from runtime.domain.models import (
            DesktopState,
            EffectsEntry,
            IconsEntry,
            PaletteEntry,
            WallpaperEntry,
        )

        install = tmp_path / "install"
        spine = _build_spine(install)
        state_root = tmp_path / "state-home" / "dotfiles"
        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(install))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))

        template_hash = canonical_hash_dir(spine["templates"])
        catalog_hash = hash_file(install / "config" / "weg" / "effects.yaml")
        icon_templates_hash = canonical_hash_dir(install / "icon-templates")
        icon_mappings_hash = hash_file(install / "icon-mappings" / "icons.yaml")

        for layer, entry_hash, fields in [
            (
                "palettes",
                PH,
                {
                    "entry_hash": PH,
                    "input_template_hash": template_hash,
                    "generated_at": "2026-09-20T00:00:00Z",
                },
            ),
            (
                "effects",
                EH,
                {"entry_hash": EH, "input_catalog_hash": catalog_hash},
            ),
            (
                "icons",
                IH,
                {
                    "entry_hash": IH,
                    "input_templates_hash": icon_templates_hash,
                    "input_mappings_hash": icon_mappings_hash,
                },
            ),
        ]:
            entry_dir = state_root / "cache" / layer / entry_hash
            entry_dir.mkdir(parents=True)
            (entry_dir / "meta.json").write_text(
                json.dumps(
                    {"hash_algorithm": "sha256", "kind": layer.rstrip("s"), **fields},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            (entry_dir / "artifact.bin").write_bytes(b"artifact")

        # Old palette entries (days 1..6) for the removal plan; keep=5 keeps
        # the 5 newest dated entries, so days 1..2 are reclaimable.
        for day in range(1, 7):
            old_hash = f"{day:064x}"
            old_dir = state_root / "cache" / "palettes" / old_hash
            old_dir.mkdir(parents=True)
            (old_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "hash_algorithm": "sha256",
                        "kind": "palette",
                        "entry_hash": old_hash,
                        "generated_at": f"2026-09-{day:02d}T00:00:00Z",
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            (old_dir / "artifact.bin").write_bytes(b"old")

        JsonStateRepository(state_root=state_root).save(
            DesktopState(
                schema_version=2,
                wallpaper=WallpaperEntry(
                    hash_algorithm="sha256",
                    kind="wallpaper",
                    content_hash=WH,
                    source_path="/img/wall.png",
                    imported_at="2026-09-10T00:00:00Z",
                ),
                monitors={},
                palette=PaletteEntry(
                    hash_algorithm="sha256",
                    kind="palette",
                    entry_hash=PH,
                    source_wallpaper_hash=WH,
                    input_template_hash=template_hash,
                    artifact_hashes={
                        "colors.yaml": "e" * 64,
                        "colors.conf": "e" * 64,
                        "colors.gtk.css": "e" * 64,
                        "colors.adw.css": "e" * 64,
                        "colors.sequences": "e" * 64,
                        "colors.rasi": "e" * 64,
                    },
                    generated_at="2026-09-10T00:00:00Z",
                ),
                effects=EffectsEntry(
                    hash_algorithm="sha256",
                    kind="effects",
                    entry_hash=EH,
                    source_wallpaper_hash=WH,
                    input_catalog_hash=catalog_hash,
                    artifact_hashes={},
                    generated_at="2026-09-10T00:00:00Z",
                ),
                icons=IconsEntry(
                    hash_algorithm="sha256",
                    kind="icons",
                    entry_hash=IH,
                    source_palette_hash=PH,
                    input_templates_hash=icon_templates_hash,
                    input_mappings_hash=icon_mappings_hash,
                    artifact_hashes={},
                    generated_at="2026-09-10T00:00:00Z",
                ),
                applied_at="2026-09-10T00:00:00Z",
            )
        )
        return state_root, install, spine

    def test_plan_reports_stale_plus_removals(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root, install, spine = self._seed(monkeypatch, tmp_path)
        (spine["templates"] / "b.j2").write_text("template-b", encoding="utf-8")
        before = (_snapshot(state_root), _snapshot(install))
        result = runner.invoke(app, ["reconcile", "--plan", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["stale"] == ["icons", "palettes"]
        assert payload["fresh"] == ["effects"]
        assert payload["removals"]["palettes"] == [f"{1:064x}", f"{2:064x}"]
        assert payload["total_removable"] == 2
        assert payload["keep"] == 5
        assert payload["prune_pinned"] is False
        assert (_snapshot(state_root), _snapshot(install)) == before
        assert not (state_root / ".seed.lock").exists()
        assert list(state_root.rglob("history.jsonl")) == []

    def test_stale_matches_check_inputs_on_same_fixture(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Differential pin: plan stale/fresh equals check-inputs stale/fresh."""
        _, _, spine = self._seed(monkeypatch, tmp_path)
        (spine["templates"] / "b.j2").write_text("template-b", encoding="utf-8")
        plan_payload = json.loads(
            runner.invoke(app, ["reconcile", "--plan", "--format", "json"]).output
        )
        check_payload = json.loads(
            runner.invoke(app, ["reconcile", "--check-inputs", "--format", "json"]).output
        )
        assert plan_payload["stale"] == check_payload["stale"]
        assert plan_payload["fresh"] == check_payload["fresh"]

    def test_fresh_with_leftovers_reports_reclaimable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root, _, _ = self._seed(monkeypatch, tmp_path)
        result = runner.invoke(app, ["reconcile", "--plan", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["stale"] == []
        assert payload["total_removable"] == 2
        plain = runner.invoke(app, ["reconcile", "--plan"])
        assert plain.exit_code == 0
        assert "all layers fresh" in plain.output
        assert "reclaimable: 2" in plain.output

    def test_stale_with_clean_cache_reports_no_removals(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Stale template edit, no old entries: stale set only, zero reclaimable."""
        state_root, _, spine = self._seed(monkeypatch, tmp_path)
        for day in range(1, 7):
            old_dir = state_root / "cache" / "palettes" / f"{day:064x}"
            for child in old_dir.rglob("*"):
                if child.is_file() or child.is_symlink():
                    child.unlink()
            old_dir.rmdir()
        (spine["templates"] / "b.j2").write_text("template-b", encoding="utf-8")
        result = runner.invoke(app, ["reconcile", "--plan", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["stale"] == ["icons", "palettes"]
        assert payload["total_removable"] == 0
        plain = runner.invoke(app, ["reconcile", "--plan"])
        assert plain.exit_code == 0
        assert "nothing reclaimable" in plain.output

    def test_corrupt_history_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root, _, _ = self._seed(monkeypatch, tmp_path)
        (state_root / "history.jsonl").write_text("{corrupt}\n", encoding="utf-8")
        result = runner.invoke(app, ["reconcile", "--plan"])
        assert result.exit_code == 1

    def test_flag_passthrough_matches_prune_dry_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root, _, _ = self._seed(monkeypatch, tmp_path)
        plan_result = runner.invoke(app, ["reconcile", "--plan", "--keep", "1", "--format", "json"])
        prune_result = runner.invoke(
            app, ["inspect", "cache", "prune", "--dry-run", "--keep", "1", "--format", "json"]
        )
        assert plan_result.exit_code == 0
        assert prune_result.exit_code == 0
        plan_payload = json.loads(plan_result.output)
        prune_payload = json.loads(prune_result.output)
        assert plan_payload["removals"] == prune_payload["removals"]
        assert plan_payload["kept"] == prune_payload["kept"]
        assert plan_payload["total_removable"] == prune_payload["total_removable"]

    def test_prune_pinned_passthrough_matches_prune_dry_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._seed(monkeypatch, tmp_path)
        plan_result = runner.invoke(
            app, ["reconcile", "--plan", "--prune-pinned", "--format", "json"]
        )
        prune_result = runner.invoke(
            app, ["inspect", "cache", "prune", "--dry-run", "--prune-pinned", "--format", "json"]
        )
        assert plan_result.exit_code == 0
        assert prune_result.exit_code == 0
        assert (
            json.loads(plan_result.output)["removals"]
            == json.loads(prune_result.output)["removals"]
        )

    def test_absent_state_never_seeds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root = tmp_path / "state-home" / "dotfiles"
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
        result = runner.invoke(app, ["reconcile", "--plan"])
        assert result.exit_code == 1
        assert "no runtime state recorded" in (result.output + getattr(result, "stderr", ""))
        assert not (state_root / "current.json").exists()
        assert list(state_root.rglob("cache")) == []
        assert not (state_root / ".seed.lock").exists()
        assert list(state_root.glob("current.json.tmp.*")) == []


def test_reconcile_without_flag_still_converges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No flags: plain reconcile takes the swap path, never the plan path."""
    plan_calls: list[None] = []

    def _swap() -> object:
        raise RuntimeError("swap path taken")

    def _nope(keep: int = 5, prune_pinned: bool = False) -> _ReconcilePlanResult:
        plan_calls.append(None)
        raise AssertionError("plan path must not run without the flag")

    monkeypatch.setattr(cli_main, "_run_reconcile", _swap)
    monkeypatch.setattr(cli_main, "_run_reconcile_plan", _nope)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 1
    assert plan_calls == []
