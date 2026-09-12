"""Unit tests for the Story 2.1 doctor three-way check (DoctorUseCase.check).

Hand-built tmp state (cache entries + current.json via the real
JsonStateRepository + hand-created current/ links), then broken per
divergence class. Covers Story 2.1 ACs: clean exit 0, per-class verdicts,
zero mutations, absent loudness. Mirrors test_cli_check_inputs.py patterns —
CliRunner + monkeypatched composition for shape, real wiring for behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.application.doctor import DoctorReport, DoctorUseCase
from runtime.cli.main import app

runner = CliRunner()

WH = "dd" * 32
PH = "aa" * 32
EH = "bb" * 32
IH = "cc" * 32
TS = "2026-09-10T00:00:00Z"

PALETTE_FILES = (
    "colors.conf",
    "colors.gtk.css",
    "colors.yaml",
    "colors.adw.css",
    "colors.sequences",
    "colors.rasi",
)


@pytest.fixture(autouse=True)
def _quiet_seed_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point first-run seeding at an empty spine so it skips quietly.

    R-3: doctor now also checks derivation-input provenance, so give the fake
    spine the four inputs (healthy) — a missing input is a real defect and is
    tested separately.
    """
    spine = tmp_path / "install"
    (spine / "icon-templates").mkdir(parents=True)
    (spine / "icon-templates" / "x.svg").write_text("<svg/>", encoding="utf-8")
    (spine / "icon-mappings").mkdir(parents=True)
    (spine / "icon-mappings" / "icons.yaml").write_text("icons: {}\n", encoding="utf-8")
    templates = spine / "config" / "color-scheme-generator" / "templates"
    templates.mkdir(parents=True)
    (templates / "t.j2").write_text("x", encoding="utf-8")
    weg = spine / "config" / "weg"
    weg.mkdir(parents=True)
    (weg / "effects.yaml").write_text("effects: {}\n", encoding="utf-8")
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(spine))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_meta(entry_dir: Path, fields: dict[str, object]) -> None:
    entry_dir.mkdir(parents=True, exist_ok=True)
    payload = {"hash_algorithm": "sha256", **fields}
    (entry_dir / "meta.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


def _seed_state(state_root: Path) -> None:
    """Build a fully consistent warm state: entries + current.json + links."""
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.domain.models import (
        BackendType,
        DesktopState,
        EffectsEntry,
        FitMode,
        IconsEntry,
        MonitorWallpaperConfig,
        PaletteEntry,
        WallpaperEntry,
    )

    wall_dir = state_root / "cache" / "wallpapers" / WH
    wall_dir.mkdir(parents=True)
    (wall_dir / "wallpaper.png").write_bytes(b"wallpaper bytes")
    _write_meta(
        wall_dir,
        {
            "kind": "wallpaper",
            "content_hash": WH,
            "source_path": "/img/wall.png",
            "imported_at": TS,
        },
    )
    pal_dir = state_root / "cache" / "palettes" / PH
    pal_artifacts = {name: _h(name.encode()) for name in PALETTE_FILES}
    for name in PALETTE_FILES:
        (pal_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (pal_dir / name).write_bytes(name.encode())
    _write_meta(
        pal_dir,
        {
            "kind": "palette",
            "entry_hash": PH,
            "source_wallpaper_hash": WH,
            "input_template_hash": "t" * 64,
            "artifact_hashes": pal_artifacts,
            "generated_at": TS,
        },
    )
    eff_dir = state_root / "cache" / "effects" / EH
    eff_dir.mkdir(parents=True)
    (eff_dir / "fx.png").write_bytes(b"fx")
    _write_meta(
        eff_dir,
        {
            "kind": "effects",
            "entry_hash": EH,
            "source_wallpaper_hash": WH,
            "input_catalog_hash": "c" * 64,
            "artifact_hashes": {"fx.png": _h(b"fx")},
            "generated_at": TS,
        },
    )
    ico_dir = state_root / "cache" / "icons" / IH
    ico_dir.mkdir(parents=True)
    (ico_dir / "icon.svg").write_bytes(b"<svg/>")
    _write_meta(
        ico_dir,
        {
            "kind": "icons",
            "entry_hash": IH,
            "source_palette_hash": PH,
            "input_templates_hash": "i" * 64,
            "input_mappings_hash": "m" * 64,
            "artifact_hashes": {"icon.svg": _h(b"<svg/>")},
            "generated_at": TS,
        },
    )
    JsonStateRepository(state_root=state_root).save(
        DesktopState(
            schema_version=2,
            wallpaper=WallpaperEntry(
                hash_algorithm="sha256",
                kind="wallpaper",
                content_hash=WH,
                source_path="/img/wall.png",
                imported_at=TS,
            ),
            monitors={
                "DP-1": MonitorWallpaperConfig(
                    backend=BackendType.hyprpaper,
                    source_hash=WH,
                    fit_mode=FitMode.cover,
                    mpv_options=None,
                    ipc_socket=None,
                )
            },
            palette=PaletteEntry(
                hash_algorithm="sha256",
                kind="palette",
                entry_hash=PH,
                source_wallpaper_hash=WH,
                input_template_hash="t" * 64,
                artifact_hashes={
                    "colors_yaml": "e" * 64,
                    "colors_conf": "e" * 64,
                    "colors_gtk_css": "e" * 64,
                    "colors_adw_css": "e" * 64,
                    "colors_sequences": "e" * 64,
                    "colors_rasi": "e" * 64,
                },
                generated_at=TS,
            ),
            effects=EffectsEntry(
                hash_algorithm="sha256",
                kind="effects",
                entry_hash=EH,
                source_wallpaper_hash=WH,
                input_catalog_hash="c" * 64,
                artifact_hashes={},
                generated_at=TS,
            ),
            icons=IconsEntry(
                hash_algorithm="sha256",
                kind="icons",
                entry_hash=IH,
                source_palette_hash=PH,
                input_templates_hash="i" * 64,
                input_mappings_hash="m" * 64,
                artifact_hashes={},
                generated_at=TS,
            ),
            applied_at=TS,
        )
    )
    current = state_root / "current"
    current.mkdir(parents=True)
    os.symlink(wall_dir / "wallpaper.png", current / "wallpaper-DP-1.png")
    os.symlink(wall_dir / "wallpaper.png", current / "wallpaper.png")
    for name in PALETTE_FILES:
        os.symlink(pal_dir / name, current / name)
    os.symlink(eff_dir, current / "effects", target_is_directory=True)
    os.symlink(ico_dir, current / "icons", target_is_directory=True)
    # R-5: the seeded store is audited — the history tail agrees with
    # current.json, so the clean-machine baseline stays clean.
    from runtime.adapters.seeder import CacheSeeder

    CacheSeeder(state_root).append_history(
        trigger="set",
        wallpaper_hash=WH,
        palette_hash=PH,
        effects_hash=EH,
        icons_hash=IH,
        source_path="/img/wall.png",
    )


def _check(state_root: Path) -> DoctorReport:
    from runtime.adapters.json_state_repository import JsonStateRepository

    return DoctorUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
    ).check()


def _by_name(report: DoctorReport) -> dict[str, str]:
    return {item.name: item.status for item in report.items}


class TestCleanMachine:
    def test_consistent_state_reports_clean(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _seed_state(state_root)
        report = _check(state_root)
        assert report.clean is True
        assert report.items and all(item.status == "ok" for item in report.items)


class TestDivergenceClasses:
    def test_missing_entry_dir(self, tmp_path: Path) -> None:
        import shutil

        state_root = tmp_path / "state"
        _seed_state(state_root)
        shutil.rmtree(state_root / "cache" / "palettes" / PH)
        report = _check(state_root)
        assert report.clean is False
        assert _by_name(report)[f"cache/palettes/{PH[:12]}"] == "missing"

    def test_corrupt_meta_is_diverged(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _seed_state(state_root)
        (state_root / "cache" / "effects" / EH / "meta.json").write_text(
            "{not json", encoding="utf-8"
        )
        report = _check(state_root)
        assert report.clean is False
        assert _by_name(report)[f"cache/effects/{EH[:12]}"] == "diverged"

    def test_absent_listed_artifact_is_diverged(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _seed_state(state_root)
        (state_root / "cache" / "palettes" / PH / "colors.conf").unlink()
        report = _check(state_root)
        assert report.clean is False
        assert _by_name(report)[f"cache/palettes/{PH[:12]}"] == "diverged"

    def test_stray_symlink_is_diverged(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _seed_state(state_root)
        stray = tmp_path / "stray.conf"
        stray.write_text("stray", encoding="utf-8")
        current = state_root / "current"
        (current / "colors.conf").unlink()
        os.symlink(stray, current / "colors.conf")
        report = _check(state_root)
        assert report.clean is False
        assert _by_name(report)["current/colors.conf"] == "diverged"

    def test_dangling_symlink_is_dangling(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _seed_state(state_root)
        (state_root / "cache" / "palettes" / PH / "colors.gtk.css").unlink()
        report = _check(state_root)
        assert report.clean is False
        by_name = _by_name(report)
        assert by_name["current/colors.gtk.css"] == "dangling"
        assert by_name[f"cache/palettes/{PH[:12]}"] == "diverged"

    def test_non_utf8_meta_is_diverged(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _seed_state(state_root)
        (state_root / "cache" / "icons" / IH / "meta.json").write_bytes(b"\xff\xfe{bad")
        report = _check(state_root)
        assert report.clean is False
        assert _by_name(report)[f"cache/icons/{IH[:12]}"] == "diverged"

    def test_file_instead_of_symlink_is_missing(self, tmp_path: Path) -> None:
        """Inspect-identical vocabulary pin: non-symlink reads missing (not diverged)."""
        state_root = tmp_path / "state"
        _seed_state(state_root)
        (state_root / "current" / "colors.conf").unlink()
        (state_root / "current" / "colors.conf").write_text("squat", encoding="utf-8")
        report = _check(state_root)
        assert report.clean is False
        assert _by_name(report)["current/colors.conf"] == "missing"

    def test_extra_file_in_current_ignored(self, tmp_path: Path) -> None:
        """Pinned decision: unexpected current/ files are inert (fixed-name
        consumers) and unactionable (repair never deletes) — clean stays True."""
        state_root = tmp_path / "state"
        _seed_state(state_root)
        (state_root / "current" / "stray.txt").write_text("junk", encoding="utf-8")
        report = _check(state_root)
        assert report.clean is True

    def test_malformed_map_is_diverged(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _seed_state(state_root)
        meta_path = state_root / "cache" / "effects" / EH / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["artifact_hashes"] = ["fx.png"]
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        report = _check(state_root)
        assert report.clean is False
        assert _by_name(report)[f"cache/effects/{EH[:12]}"] == "diverged"

    def test_unsafe_artifact_key_is_diverged(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _seed_state(state_root)
        meta_path = state_root / "cache" / "icons" / IH / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["artifact_hashes"] = {"/etc/passwd": "ab" * 32}
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        report = _check(state_root)
        assert report.clean is False
        assert _by_name(report)[f"cache/icons/{IH[:12]}"] == "diverged"

    def test_removed_link_is_missing(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _seed_state(state_root)
        (state_root / "current" / "effects").unlink()
        report = _check(state_root)
        assert report.clean is False
        assert _by_name(report)["current/effects"] == "missing"

    def test_absent_state_raises(self, tmp_path: Path) -> None:
        from runtime.adapters.json_state_repository import JsonStateRepository

        with pytest.raises(ValueError, match="no runtime state recorded"):
            DoctorUseCase(
                state_repo=JsonStateRepository(state_root=tmp_path / "nostate"),
                state_root=tmp_path / "nostate",
            ).check()


def _snapshot(root: Path) -> dict[str, str]:
    """FS-mutation pin (v2 pattern): links, dirs, modes, mtimes, hashes."""
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


class TestZeroMutation:
    @pytest.mark.parametrize(
        "breakage", ["none", "missing-entry", "stray-link", "diverged-history", "absent-history"]
    )
    def test_check_mutates_nothing(self, tmp_path: Path, breakage: str) -> None:
        import shutil

        state_root = tmp_path / "state"
        _seed_state(state_root)
        if breakage == "missing-entry":
            shutil.rmtree(state_root / "cache" / "icons" / IH)
        elif breakage == "stray-link":
            (state_root / "current" / "icons").unlink()
            stray = tmp_path / "stray-icons"
            stray.mkdir()
            os.symlink(stray, state_root / "current" / "icons")
        elif breakage == "diverged-history":
            history_path = state_root / "history.jsonl"
            stale = json.loads(history_path.read_text(encoding="utf-8").splitlines()[0])
            stale["wallpaper"] = "ee" * 32
            history_path.write_text(json.dumps(stale) + "\n", encoding="utf-8")
        elif breakage == "absent-history":
            (state_root / "history.jsonl").unlink()
        before = _snapshot(state_root)
        _check(state_root)
        assert _snapshot(state_root) == before


class TestCliShape:
    def _report(self, clean: bool) -> DoctorReport:
        from runtime.application.doctor import DriftItem

        items = (
            (DriftItem("cache/palettes/aa", "entry", "ok", "healthy"),)
            if clean
            else (
                DriftItem("cache/palettes/aa", "entry", "ok", "healthy"),
                DriftItem("current/colors.conf", "symlink", "diverged", "stray"),
            )
        )
        return DoctorReport(items=items, clean=clean)

    def test_clean_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_main, "_run_doctor_check", lambda: self._report(True))
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "desktop clean" in result.output

    def test_drift_exits_nonzero_with_report(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_main, "_run_doctor_check", lambda: self._report(False))
        result = runner.invoke(app, ["doctor", "--format", "json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["clean"] is False
        assert {item["name"]: item["status"] for item in payload["items"]} == {
            "cache/palettes/aa": "ok",
            "current/colors.conf": "diverged",
        }

    def test_absent_state_never_seeds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root = tmp_path / "state-home" / "dotfiles"
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "no runtime state recorded" in (result.output + getattr(result, "stderr", ""))
        assert not (state_root / "current.json").exists()
        assert not (state_root / ".seed.lock").exists()

    def test_clean_machine_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root = tmp_path / "state-home" / "dotfiles"
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
        _seed_state(state_root)
        before = _snapshot(state_root)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "desktop clean" in result.output
        assert _snapshot(state_root) == before

    def test_diverged_machine_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root = tmp_path / "state-home" / "dotfiles"
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
        _seed_state(state_root)
        stray = tmp_path / "stray.conf"
        stray.write_text("stray", encoding="utf-8")
        (state_root / "current" / "colors.conf").unlink()
        os.symlink(stray, state_root / "current" / "colors.conf")
        before = _snapshot(state_root)
        result = runner.invoke(app, ["doctor", "--format", "json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["clean"] is False
        assert payload["items"] and any(
            item["name"] == "current/colors.conf" and item["status"] == "diverged"
            for item in payload["items"]
        )
        assert _snapshot(state_root) == before


def test_missing_derivation_input_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """R-3/AD-43: doctor surfaces a derivation input missing from the spine."""
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "empty-spine"))
    monkeypatch.delenv("DOTFILES_DEV_INPUTS_ROOT", raising=False)
    state_root = tmp_path / "state-home" / "dotfiles"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    _seed_state(state_root)
    result = runner.invoke(app, ["doctor", "--format", "json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["clean"] is False
    assert any(
        item["name"].startswith("input:") and item["status"] == "missing"
        for item in payload["items"]
    )


def test_repo_sourced_input_is_not_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """R-3/AD-43: a repo-sourced input (override set) dirties doctor — never passes as clean."""
    repo = tmp_path / "checkout"
    (repo / "dotfiles/assets/icon-templates").mkdir(parents=True)
    (repo / "dotfiles/assets/icon-templates/x.svg").write_text("<svg/>", encoding="utf-8")
    mappings = repo / "dotfiles/config/icon-template-color-scheme-mappings"
    mappings.mkdir(parents=True)
    (mappings / "icons.yaml").write_text("{}\n", encoding="utf-8")
    templates = (
        repo / "src/cli-tools/color-scheme-generator/src/color_scheme_generator/defaults/templates"
    )
    templates.mkdir(parents=True)
    (templates / "t.j2").write_text("x", encoding="utf-8")
    weg = (
        repo / "src/cli-tools/wallpaper-effects-generator/src/wallpaper_effects_generator/defaults"
    )
    weg.mkdir(parents=True)
    (weg / "effects.yaml").write_text("{}\n", encoding="utf-8")

    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "empty-spine"))
    monkeypatch.setenv("DOTFILES_DEV_INPUTS_ROOT", str(repo))
    state_root = tmp_path / "state-home" / "dotfiles"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    _seed_state(state_root)

    result = runner.invoke(app, ["doctor", "--format", "json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["clean"] is False
    assert any(
        item["name"].startswith("input:") and item["status"] == "diverged"
        for item in payload["items"]
    )


class TestStoreHistoryDivergence:
    """R-5/AD-41: store vs history-tail agreement leg."""

    def test_matching_tail_is_ok(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _seed_state(state_root)
        report = _check(state_root)
        assert report.clean is True
        assert {i.name: i.status for i in report.items if i.kind == "history"} == {
            "history:tail": "ok"
        }

    def test_absent_history_is_diverged(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _seed_state(state_root)
        (state_root / "history.jsonl").unlink()
        report = _check(state_root)
        assert report.clean is False
        assert _by_name(report)["history:tail"] == "diverged"

    def test_store_ahead_of_tail_is_diverged_and_pure(self, tmp_path: Path) -> None:
        """Faithful crash window: the history ends at an older record."""
        state_root = tmp_path / "state"
        _seed_state(state_root)
        lines = (state_root / "history.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        stale = {**json.loads(lines[0]), "wallpaper": "ee" * 32}
        (state_root / "history.jsonl").write_text(json.dumps(stale) + "\n", encoding="utf-8")
        report = _check(state_root)
        assert _by_name(report)["history:tail"] == "diverged"
        assert [name for name, status in _by_name(report).items() if status != "ok"] == [
            "history:tail"
        ]
        item = next(i for i in report.items if i.name == "history:tail")
        assert "wallpaper" in item.detail

    def test_applied_at_churn_stays_clean(self, tmp_path: Path) -> None:
        """Timestamps are not identity: a re-save with fresh applied_at is clean."""
        import dataclasses

        from runtime.adapters.json_state_repository import JsonStateRepository

        state_root = tmp_path / "state"
        _seed_state(state_root)
        repo = JsonStateRepository(state_root=state_root)
        state = repo.load_current()
        assert state is not None
        repo.save(dataclasses.replace(state, applied_at="2026-09-11T00:00:00Z"))
        assert _check(state_root).clean is True

    def test_newer_trigger_and_details_stays_clean(self, tmp_path: Path) -> None:
        """Trigger values and details are not identity: a prune line on top is clean."""
        from runtime.adapters.seeder import CacheSeeder

        state_root = tmp_path / "state"
        _seed_state(state_root)
        CacheSeeder(state_root).append_history(
            trigger="prune",
            wallpaper_hash=WH,
            palette_hash=PH,
            effects_hash=EH,
            icons_hash=IH,
            source_path="/img/wall.png",
            details={"removed": 3, "layers": {"effects": 3}},
        )
        assert _check(state_root).clean is True

    def test_corrupt_middle_history_line_fails_loud(self, tmp_path: Path) -> None:
        from runtime.adapters.seeder import CacheSeeder

        state_root = tmp_path / "state"
        _seed_state(state_root)
        with (state_root / "history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{corrupt\n")
        CacheSeeder(state_root).append_history(
            trigger="set",
            wallpaper_hash=WH,
            palette_hash=PH,
            effects_hash=EH,
            icons_hash=IH,
            source_path="/img/wall.png",
        )
        with pytest.raises(ValueError, match=r"history\.jsonl line 2"):
            _check(state_root)

    def test_symlinked_history_fails_loud(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _seed_state(state_root)
        real = state_root / "history.jsonl"
        shadow = tmp_path / "shadow-history.jsonl"
        shadow.write_bytes(real.read_bytes())
        real.unlink()
        os.symlink(shadow, real)
        with pytest.raises(ValueError, match="symlink"):
            _check(state_root)

    def test_torn_tail_tolerated_through_check(self, tmp_path: Path) -> None:
        """Item 2(a): a torn trailing line is skipped; the parseable tail decides."""
        state_root = tmp_path / "state"
        _seed_state(state_root)
        with (state_root / "history.jsonl").open("ab") as handle:
            handle.write(b'{"ts": "2026-09-')
        before = (state_root / "history.jsonl").read_bytes()
        report = _check(state_root)
        assert _by_name(report)["history:tail"] == "ok"
        assert (state_root / "history.jsonl").read_bytes() == before

    def test_reactive_tail_stays_clean(self, tmp_path: Path) -> None:
        """Item 2(d): the daemon's trigger value is not identity."""
        from runtime.adapters.seeder import CacheSeeder

        state_root = tmp_path / "state"
        _seed_state(state_root)
        CacheSeeder(state_root).append_history(
            trigger="reactive",
            wallpaper_hash=WH,
            palette_hash=PH,
            effects_hash=EH,
            icons_hash=IH,
            source_path="/img/wall.png",
        )
        assert _check(state_root).clean is True

    def test_source_path_variance_stays_clean(self, tmp_path: Path) -> None:
        """Item 2(e): provenance churn is not identity."""
        import json as json_module

        state_root = tmp_path / "state"
        _seed_state(state_root)
        history_path = state_root / "history.jsonl"
        record = json.loads(history_path.read_text(encoding="utf-8").splitlines()[0])
        record["source_path"] = "/relocated/wall.png"
        history_path.write_text(json_module.dumps(record) + "\n", encoding="utf-8")
        assert _check(state_root).clean is True

    def test_details_only_variance_stays_clean(self, tmp_path: Path) -> None:
        """Item 2(f): same trigger, details differing — still clean."""
        from runtime.adapters.seeder import CacheSeeder

        state_root = tmp_path / "state"
        _seed_state(state_root)
        CacheSeeder(state_root).append_history(
            trigger="set",
            wallpaper_hash=WH,
            palette_hash=PH,
            effects_hash=EH,
            icons_hash=IH,
            source_path="/img/wall.png",
            details={"removed": 0, "layers": {}},
        )
        assert _check(state_root).clean is True
