"""Unit tests for ApplyWallpaperUseCase (Story 1.13).

Covers: new-wallpaper happy path (entries created, csg/weg/itr invoked
once each, current.json updated with real hashes + absolute source_path);
same-img re-set (cache hits, applied_at refreshed); previously-used
wallpaper re-set; palette failure aborts (current.json unchanged);
effects/icons failure degrades (nulls); invalid inputs raise; monitors
preserved/defaulted; and the AC 6 scope boundary (no symlink repoint,
no history append).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.hashing import (
    canonical_hash_dir,
    effects_entry_hash,
    hash_file,
    icons_entry_hash,
    palette_entry_hash,
)
from runtime.adapters.seeder import CacheSeeder
from runtime.domain.models import (
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
    WallpaperEntry,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
WALLPAPER_PNG = FIXTURES / "wallpaper.png"


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _FakeStateRepo:
    """Fake IStateRepository for testing."""

    def __init__(self, state: DesktopState | None = None) -> None:
        self._state = state
        self.saved: list[DesktopState] = []

    def load_current(self) -> DesktopState | None:
        return self._state

    def save(self, state: DesktopState) -> None:
        self._state = state
        self.saved.append(state)


class _FakeCsg:
    """Contract-honest fake: echoes the output dir's name as entry hash."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("csg exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "colors.yaml").write_text("colors: []")
        (output_dir / "colors.conf").write_text("colors {}")
        (output_dir / "colors.gtk.css").write_text("colors {}")
        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_template_hash="c" * 64,
            artifact_hashes=PaletteArtifacts(
                colors_yaml=hash_file(output_dir / "colors.yaml"),
                colors_conf=hash_file(output_dir / "colors.conf"),
                colors_gtk_css=hash_file(output_dir / "colors.gtk.css"),
            ),
            generated_at=_now_z(),
        )


class _FakeWeg:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("weg exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "effect.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        effect_hash = hash_file(output_dir / "effect.png")
        return EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_catalog_hash="2" * 64,
            artifact_hashes=EffectsArtifacts(**{"effect.png": effect_hash}),
            generated_at=_now_z(),
        )


class _FakeItr:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def render(
        self,
        palette_hash: str,
        templates_dir: Path,
        mappings_path: Path,
        output_dir: Path,
    ) -> Any:
        from runtime.domain.models import IconsArtifacts, IconsEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("itr exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=output_dir.name,
            source_palette_hash=palette_hash,
            input_templates_hash="5" * 64,
            input_mappings_hash="6" * 64,
            artifact_hashes=IconsArtifacts(**{"icon.svg": hash_file(output_dir / "icon.svg")}),
            generated_at=_now_z(),
        )


def _setup_spine(install_spine: Path) -> None:
    """Create spine config inputs (templates/catalog/icon assets)."""
    csg_templates = install_spine / "config" / "color-scheme-generator" / "templates"
    csg_templates.mkdir(parents=True)
    (csg_templates / "default.yaml").write_text("window: {}\n")
    weg_config = install_spine / "config" / "weg"
    weg_config.mkdir(parents=True)
    (weg_config / "effects.yaml").write_text("effects: []\n")
    itr_templates = install_spine / "config" / "icon-templates-renderer" / "templates"
    itr_templates.mkdir(parents=True)
    (itr_templates / "terminal.svg").write_text("<svg/>")
    (install_spine / "config" / "icon-templates-renderer" / "icons.yaml").write_text(
        "icons: {}\n"
    )


def _make_use_case(
    tmp_path: Path,
    repo: _FakeStateRepo,
    *,
    csg: _FakeCsg | None = None,
    weg: _FakeWeg | None = None,
    itr: _FakeItr | None = None,
    install_spine: Path | None = None,
) -> Any:
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    state_root = tmp_path / "state"
    spine = install_spine if install_spine is not None else tmp_path / "install"
    return ApplyWallpaperUseCase(
        state_repo=repo,
        csg=csg or _FakeCsg(),
        weg=weg or _FakeWeg(),
        itr=itr or _FakeItr(),
        install_spine=spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
    )


def _img_in(tmp_path: Path, name: str, content: bytes) -> Path:
    img = tmp_path / name
    img.write_bytes(content)
    return img


def _spine_paths(tmp_path: Path) -> dict[str, str]:
    install_spine = tmp_path / "install"
    return {
        "template_set_hash": canonical_hash_dir(
            install_spine / "config" / "color-scheme-generator" / "templates"
        ),
        "catalog_hash": hash_file(install_spine / "config" / "weg" / "effects.yaml"),
    }


class TestApplyWallpaperHappyPath:
    """AC 1: new wallpaper — derive once, cache, persist."""

    def test_new_wallpaper_invokes_each_tool_once(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        csg, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()
        use_case = _make_use_case(tmp_path, repo, csg=csg, weg=weg, itr=itr)
        img = _img_in(tmp_path, "wall.png", b"new wallpaper bytes")

        result = use_case.run(img)

        assert (csg.calls, weg.calls, itr.calls) == (1, 1, 1)
        assert not result.cache_hit_palette
        assert not result.cache_hit_effects
        assert not result.cache_hit_icons
        assert len(repo.saved) == 1

    def test_state_carries_real_hashes_and_absolute_source_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo)
        img = _img_in(tmp_path, "wall.png", b"new wallpaper bytes")
        monkeypatch.chdir(tmp_path)  # relative input must resolve to absolute

        result = use_case.run(Path("wall.png"))

        state = repo.saved[0]
        wh = hash_file(img)
        assert state.schema_version == 2
        assert state.wallpaper.content_hash == wh
        assert state.wallpaper.source_path == str(img)  # absolute, resolved
        hashes = _spine_paths(tmp_path)
        peh = palette_entry_hash(wh, hashes["template_set_hash"])
        eeh = effects_entry_hash(wh, hashes["catalog_hash"])
        assert state.palette is not None and state.palette.entry_hash == peh
        assert state.effects is not None and state.effects.entry_hash == eeh
        assert state.icons is not None
        assert state.icons.source_palette_hash == peh
        assert result.wallpaper_hash == wh

    def test_cache_entry_dirs_named_by_computed_entry_hashes(
        self, tmp_path: Path
    ) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo)
        img = _img_in(tmp_path, "wall.png", b"hash-addressed bytes")
        use_case.run(img)

        state = repo.saved[0]
        wh = state.wallpaper.content_hash
        assert state.palette is not None
        assert state.effects is not None
        assert state.icons is not None
        assert (tmp_path / "state" / "cache" / "wallpapers" / wh).is_dir()
        assert (
            tmp_path / "state" / "cache" / "palettes" / state.palette.entry_hash
        ).is_dir()
        assert (
            tmp_path / "state" / "cache" / "effects" / state.effects.entry_hash
        ).is_dir()
        assert (tmp_path / "state" / "cache" / "icons" / state.icons.entry_hash).is_dir()

    def test_meta_json_matches_shared_data_contract_schemas(
        self, tmp_path: Path
    ) -> None:
        """The staging-written meta.json (renamed to target) matches schemas."""
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo)
        img = _img_in(tmp_path, "wall.png", b"schema bytes")
        use_case.run(img)

        state = repo.saved[0]
        assert state.palette is not None
        assert state.effects is not None
        assert state.icons is not None
        wh = state.wallpaper.content_hash

        wmeta = json.loads(
            (tmp_path / "state" / "cache" / "wallpapers" / wh / "meta.json").read_text()
        )
        assert wmeta == {
            "hash_algorithm": "sha256",
            "kind": "wallpaper",
            "content_hash": wh,
            "source_path": str(img),
            "imported_at": wmeta["imported_at"],
        }
        assert wmeta["imported_at"].endswith("Z")

        pmeta = json.loads(
            (
                tmp_path / "state" / "cache" / "palettes" / state.palette.entry_hash / "meta.json"
            ).read_text()
        )
        assert pmeta["hash_algorithm"] == "sha256"
        assert pmeta["kind"] == "palette"
        assert pmeta["entry_hash"] == state.palette.entry_hash
        assert pmeta["source_wallpaper_hash"] == wh
        assert set(pmeta["artifact_hashes"]) == {"colors.yaml", "colors.conf", "colors.gtk.css"}
        for name, h in pmeta["artifact_hashes"].items():
            assert h == hash_file(
                tmp_path / "state" / "cache" / "palettes" / state.palette.entry_hash / name
            )
        assert pmeta["generated_at"].endswith("Z")

    def test_wallpaper_meta_written_only_for_new_entries(self, tmp_path: Path) -> None:
        """Cache entries are write-once: re-set must not rewrite meta.json."""
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo)
        img = _img_in(tmp_path, "wall.png", b"write-once bytes")
        use_case.run(img)
        wh = hash_file(img)
        meta_path = tmp_path / "state" / "cache" / "wallpapers" / wh / "meta.json"
        first_meta = meta_path.read_text()

        use_case.run(img)

        assert meta_path.read_text() == first_meta


class TestApplyWallpaperCacheHits:
    """AC 2/AC 3: re-sets are cache hits with zero tool invocations."""

    def test_same_image_reset_is_full_cache_hit(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        csg, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()
        use_case = _make_use_case(tmp_path, repo, csg=csg, weg=weg, itr=itr)
        img = _img_in(tmp_path, "wall.png", b"repeat bytes")

        first = use_case.run(img)
        csg.calls = weg.calls = itr.calls = 0
        second = use_case.run(img)

        assert (csg.calls, weg.calls, itr.calls) == (0, 0, 0)
        assert second.cache_hit_palette
        assert second.cache_hit_effects
        assert second.cache_hit_icons
        assert second.palette.entry_hash == first.palette.entry_hash
        assert second.state.applied_at >= first.state.applied_at  # refreshed

    def test_previously_used_wallpaper_reset_is_cache_hit(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        csg, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()
        use_case = _make_use_case(tmp_path, repo, csg=csg, weg=weg, itr=itr)
        img_a = _img_in(tmp_path, "a.png", b"wallpaper a")
        img_b = _img_in(tmp_path, "b.png", b"wallpaper b")

        first = use_case.run(img_a)
        use_case.run(img_b)
        csg.calls = weg.calls = itr.calls = 0
        restored = use_case.run(img_a)

        assert (csg.calls, weg.calls, itr.calls) == (0, 0, 0)
        assert restored.state.wallpaper.content_hash == hash_file(img_a)
        assert restored.palette.entry_hash == first.palette.entry_hash
        assert restored.state.wallpaper.source_path == str(img_a)


class TestApplyWallpaperFailurePolicy:
    """AC 4: palette hard, effects/icons graceful."""

    def test_palette_failure_aborts_and_leaves_state_unchanged(
        self, tmp_path: Path
    ) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        csg = _FakeCsg(fail=True)
        use_case = _make_use_case(tmp_path, repo, csg=csg)
        img = _img_in(tmp_path, "wall.png", b"doomed bytes")

        with pytest.raises(RuntimeError, match="palette apply failed:"):
            use_case.run(img)

        assert repo.saved == []  # current.json untouched
        assert not (tmp_path / "state" / "current.json").exists()

    def test_effects_failure_degrades_to_null(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        use_case = _make_use_case(
            tmp_path, repo, weg=_FakeWeg(fail=True)
        )
        img = _img_in(tmp_path, "wall.png", b"no effects bytes")

        with caplog.at_level(logging.WARNING, logger="runtime.application.apply_wallpaper"):
            result = use_case.run(img)

        assert repo.saved == [result.state]
        assert result.state.palette is not None
        assert result.state.effects is None
        assert result.state.icons is not None
        assert any("effects" in r.message.lower() for r in caplog.records)

    def test_icons_failure_degrades_to_null(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, itr=_FakeItr(fail=True))
        img = _img_in(tmp_path, "wall.png", b"no icons bytes")

        result = use_case.run(img)

        assert result.state.palette is not None
        assert result.state.effects is not None
        assert result.state.icons is None

    def test_corrupt_state_propagates_loudly(self, tmp_path: Path) -> None:
        class _CorruptRepo:
            def load_current(self) -> DesktopState:
                raise ValueError("current.json is not valid JSON")

            def save(self, state: DesktopState) -> None:
                raise AssertionError("save must not be reached")

        _setup_spine(tmp_path / "install")
        use_case = _make_use_case(tmp_path, _CorruptRepo())  # type: ignore[arg-type]
        img = _img_in(tmp_path, "wall.png", b"corrupt-guard bytes")

        with pytest.raises(ValueError, match="not valid JSON"):
            use_case.run(img)


class TestApplyWallpaperInputValidation:
    """AC 1: loud input validation with clear messages."""

    def test_missing_file_raises_value_error(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo)
        with pytest.raises(ValueError, match="not found"):
            use_case.run(tmp_path / "missing.png")

    def test_directory_input_raises_value_error(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo)
        with pytest.raises(ValueError, match="regular file"):
            use_case.run(tmp_path)

    def test_empty_path_raises_value_error(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo)
        with pytest.raises(ValueError):
            use_case.run(Path(""))


class TestApplyWallpaperMonitors:
    """AC 5: preserve-or-default monitor configs."""

    def _state_with_monitors(self, monitors: dict[str, MonitorWallpaperConfig], wh: str) -> DesktopState:
        now = _now_z()
        return DesktopState(
            schema_version=2,
            wallpaper=WallpaperEntry(
                hash_algorithm="sha256",
                kind="wallpaper",
                content_hash=wh,
                source_path="",
                imported_at=now,
            ),
            monitors=monitors,
            palette=None,
            effects=None,
            icons=None,
            applied_at=now,
        )

    def test_monitors_preserved_with_updated_source_hash(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        old_wh = "a" * 64
        repo = _FakeStateRepo(
            self._state_with_monitors(
                {
                    "DP-1": MonitorWallpaperConfig(
                        backend=BackendType.hyprpaper,
                        source_hash=old_wh,
                        fit_mode=FitMode.cover,
                        mpv_options=None,
                        ipc_socket=None,
                    ),
                    "DP-2": MonitorWallpaperConfig(
                        backend=BackendType.swww,
                        source_hash=old_wh,
                        fit_mode=FitMode.contain,
                        mpv_options=None,
                        ipc_socket=None,
                    ),
                },
                old_wh,
            )
        )
        use_case = _make_use_case(tmp_path, repo)
        img = _img_in(tmp_path, "wall.png", b"monitor preserve bytes")
        use_case.run(img)

        state = repo.saved[0]
        new_wh = hash_file(img)
        assert set(state.monitors) == {"DP-1", "DP-2"}
        for cfg in state.monitors.values():
            assert cfg.source_hash == new_wh
        assert state.monitors["DP-1"].backend == BackendType.hyprpaper
        assert state.monitors["DP-1"].fit_mode == FitMode.cover
        assert state.monitors["DP-2"].backend == BackendType.swww
        assert state.monitors["DP-2"].fit_mode == FitMode.contain

    def test_mpvpaper_monitor_config_preserved(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo(
            self._state_with_monitors(
                {
                    "DP-1": MonitorWallpaperConfig(
                        backend=BackendType.mpvpaper,
                        source_hash="a" * 64,
                        fit_mode=FitMode.cover,
                        mpv_options="--vo=gpu",
                        ipc_socket="/tmp/mpv.sock",
                    ),
                },
                "a" * 64,
            )
        )
        use_case = _make_use_case(tmp_path, repo)
        img = _img_in(tmp_path, "wall.png", b"mpvpaper bytes")
        use_case.run(img)

        cfg = repo.saved[0].monitors["DP-1"]
        assert cfg.backend == BackendType.mpvpaper
        assert cfg.mpv_options == "--vo=gpu"
        assert cfg.ipc_socket == "/tmp/mpv.sock"
        assert cfg.source_hash == hash_file(img)

    def test_absent_state_defaults_single_monitor(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()  # no current.json
        use_case = _make_use_case(tmp_path, repo)
        img = _img_in(tmp_path, "wall.png", b"default monitors bytes")
        use_case.run(img)

        state = repo.saved[0]
        assert set(state.monitors) == {"DP-1"}
        cfg = state.monitors["DP-1"]
        assert cfg.backend == BackendType.hyprpaper
        assert cfg.fit_mode == FitMode.cover
        assert cfg.source_hash == hash_file(img)

    def test_empty_monitors_state_defaults_single_monitor(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo(self._state_with_monitors({}, "a" * 64))
        use_case = _make_use_case(tmp_path, repo)
        img = _img_in(tmp_path, "wall.png", b"empty monitors bytes")
        use_case.run(img)

        state = repo.saved[0]
        assert set(state.monitors) == {"DP-1"}
        assert state.monitors["DP-1"].backend == BackendType.hyprpaper


class TestApplyWallpaperScopeBoundary:
    """AC 6: apply does NOT repoint symlinks, append history, or reload."""

    def test_no_symlinks_and_no_history(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo)
        img = _img_in(tmp_path, "wall.png", b"scope boundary bytes")
        use_case.run(img)

        assert not (tmp_path / "state" / "current").exists()
        assert not (tmp_path / "state" / "history.jsonl").exists()
