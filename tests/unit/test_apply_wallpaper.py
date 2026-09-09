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
    """Fake IStateRepository for testing (records load/save event order)."""

    def __init__(
        self,
        state: DesktopState | None = None,
        events: list[str] | None = None,
    ) -> None:
        self._state = state
        self.saved: list[DesktopState] = []
        self.events = events if events is not None else []

    def load_current(self) -> DesktopState | None:
        self.events.append("load")
        return self._state

    def save(self, state: DesktopState) -> None:
        self.events.append("save")
        self._state = state
        self.saved.append(state)


class _FakeMutex:
    """Fake ISeedMutex: records acquire/release + blocking flag into events."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.holds = 0

    def hold(self, blocking: bool = False) -> Any:
        self.holds += 1
        self.events.append(f"acquire(blocking={blocking})")

        class _Hold:
            def __enter__(self_inner) -> None:
                return None

            def __exit__(self_inner, *exc: object) -> None:
                self.events.append("release")

        return _Hold()


class _FakeCsg:
    """Contract-honest fake: echoes the output dir's name as entry hash."""

    def __init__(self, *, fail: bool = False, events: list[str] | None = None) -> None:
        self.fail = fail
        self.calls = 0
        self.events = events

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        self.calls += 1
        if self.events is not None:
            self.events.append("csg")
        if self.fail:
            raise RuntimeError("csg exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "colors.yaml").write_text("colors: []")
        (output_dir / "colors.conf").write_text("colors {}")
        (output_dir / "colors.gtk.css").write_text("colors {}")
        (output_dir / "colors.adw.css").write_text("colors {}")
        (output_dir / "colors.sequences").write_bytes(b"\x1b]4;0;#000\x1b\\")
        (output_dir / "colors.rasi").write_text("* { background: #000; }")
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
                colors_adw_css=hash_file(output_dir / "colors.adw.css"),
                colors_sequences=hash_file(output_dir / "colors.sequences"),
                colors_rasi=hash_file(output_dir / "colors.rasi"),
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
    itr_templates = install_spine / "icon-templates"
    itr_templates.mkdir(parents=True)
    (itr_templates / "terminal.svg").write_text("<svg/>")
    (install_spine / "icon-mappings").mkdir(parents=True, exist_ok=True)
    (install_spine / "icon-mappings" / "icons.yaml").write_text("icons: {}\n")


def _make_use_case(
    tmp_path: Path,
    repo: Any,
    *,
    csg: _FakeCsg | None = None,
    weg: _FakeWeg | None = None,
    itr: _FakeItr | None = None,
    install_spine: Path | None = None,
    mutex: Any | None = None,
    monitor_source: Any | None = None,
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
        mutex=mutex if mutex is not None else _FakeMutex(),
        monitor_source=monitor_source,
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


def _existing_state(wh: str) -> DesktopState:
    """A minimal pre-existing DesktopState (one DP-1 hyprpaper monitor)."""
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
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=wh,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=None,
        effects=None,
        icons=None,
        applied_at=now,
    )


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

    def test_cache_entry_dirs_named_by_computed_entry_hashes(self, tmp_path: Path) -> None:
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
        assert (tmp_path / "state" / "cache" / "palettes" / state.palette.entry_hash).is_dir()
        assert (tmp_path / "state" / "cache" / "effects" / state.effects.entry_hash).is_dir()
        assert (tmp_path / "state" / "cache" / "icons" / state.icons.entry_hash).is_dir()

    def test_meta_json_matches_shared_data_contract_schemas(self, tmp_path: Path) -> None:
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
        assert set(pmeta["artifact_hashes"]) == {
            "colors.yaml",
            "colors.conf",
            "colors.gtk.css",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
        }
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

    def test_palette_failure_aborts_and_leaves_state_unchanged(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        existing = _existing_state("a" * 64)
        repo = _FakeStateRepo(existing)
        csg = _FakeCsg(fail=True)
        use_case = _make_use_case(tmp_path, repo, csg=csg)
        img = _img_in(tmp_path, "wall.png", b"doomed bytes")

        with pytest.raises(RuntimeError, match="palette apply failed:"):
            use_case.run(img)

        assert repo.saved == []  # no save reached
        assert repo.load_current() is existing  # current.json unchanged

    def test_palette_failure_when_spine_missing_wraps_loudly(self, tmp_path: Path) -> None:
        """Missing spine inputs surface as ``palette apply failed:`` (hard dep)."""
        repo = _FakeStateRepo()  # no spine setup at all
        use_case = _make_use_case(tmp_path, repo)
        img = _img_in(tmp_path, "wall.png", b"no spine bytes")

        with pytest.raises(RuntimeError, match="palette apply failed:.*templates"):
            use_case.run(img)

        assert repo.saved == []
        assert repo.load_current() is None

    def test_effects_failure_degrades_to_null(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _setup_spine(tmp_path / "install")
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, weg=_FakeWeg(fail=True))
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

    def _state_with_monitors(
        self, monitors: dict[str, MonitorWallpaperConfig], wh: str
    ) -> DesktopState:
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

    def test_absent_state_uses_injected_monitor_source(self, tmp_path: Path) -> None:
        """With no existing state, the injected source's outputs are used."""
        _setup_spine(tmp_path / "install")

        class _FakeMonitorSource:
            def detect_monitors(self) -> list[str]:
                return ["eDP-1", "HDMI-A-1"]

        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, monitor_source=_FakeMonitorSource())
        img = _img_in(tmp_path, "wall.png", b"detected monitors bytes")
        use_case.run(img)

        state = repo.saved[0]
        assert set(state.monitors) == {"eDP-1", "HDMI-A-1"}
        for cfg in state.monitors.values():
            assert cfg.backend == BackendType.hyprpaper
            assert cfg.fit_mode == FitMode.cover
            assert cfg.source_hash == hash_file(img)

    def test_empty_source_detection_falls_back_to_dp1(self, tmp_path: Path) -> None:
        """An injected source that cannot detect falls back to DEFAULT_MONITOR."""
        _setup_spine(tmp_path / "install")

        class _EmptyMonitorSource:
            def detect_monitors(self) -> list[str]:
                return []

        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, monitor_source=_EmptyMonitorSource())
        img = _img_in(tmp_path, "wall.png", b"fallback monitors bytes")
        use_case.run(img)

        state = repo.saved[0]
        assert set(state.monitors) == {"DP-1"}

    def test_existing_monitors_reconciled_against_detection(self, tmp_path: Path) -> None:
        """Live detection is authoritative for NAMES when it returns a set.

        Same-name configs keep their per-monitor settings; a detected name
        with no stored entry gets a default; a stored name no longer
        detected (stale — renamed output) is dropped.
        """
        _setup_spine(tmp_path / "install")

        class _FakeMonitorSource:
            def detect_monitors(self) -> list[str]:
                return ["eDP-2", "HDMI-A-1"]

        repo = _FakeStateRepo(
            self._state_with_monitors(
                {
                    "eDP-1": MonitorWallpaperConfig(  # stale (renamed to eDP-2)
                        backend=BackendType.hyprpaper,
                        source_hash="a" * 64,
                        fit_mode=FitMode.contain,
                        mpv_options=None,
                        ipc_socket=None,
                    ),
                    "HDMI-A-1": MonitorWallpaperConfig(
                        backend=BackendType.swww,
                        source_hash="a" * 64,
                        fit_mode=FitMode.contain,
                        mpv_options=None,
                        ipc_socket=None,
                    ),
                },
                "a" * 64,
            )
        )
        use_case = _make_use_case(tmp_path, repo, monitor_source=_FakeMonitorSource())
        img = _img_in(tmp_path, "wall.png", b"reconciled monitors bytes")
        use_case.run(img)

        state = repo.saved[0]
        assert set(state.monitors) == {"eDP-2", "HDMI-A-1"}
        # stale entry dropped
        assert "eDP-1" not in state.monitors
        # same-name config preserved (backend/fit), source_hash updated
        assert state.monitors["HDMI-A-1"].backend == BackendType.swww
        assert state.monitors["HDMI-A-1"].fit_mode == FitMode.contain
        assert state.monitors["HDMI-A-1"].source_hash == hash_file(img)
        # new name gets the default config
        assert state.monitors["eDP-2"].backend == BackendType.hyprpaper
        assert state.monitors["eDP-2"].fit_mode == FitMode.cover


class TestApplyWallpaperScopeBoundary:
    """AC 6: apply does NOT repoint symlinks, append history, or reload.

    Uses the REAL JsonStateRepository (not the in-memory fake) so the
    filesystem negative assertions are meaningful.
    """

    def test_no_symlinks_and_no_history(self, tmp_path: Path) -> None:
        from runtime.adapters.json_state_repository import JsonStateRepository

        _setup_spine(tmp_path / "install")
        state_root = tmp_path / "state"
        state_root.mkdir()
        repo = JsonStateRepository(state_root=state_root)
        use_case = _make_use_case(tmp_path, repo)
        img = _img_in(tmp_path, "wall.png", b"scope boundary bytes")
        use_case.run(img)

        assert (state_root / "current.json").is_file()  # only artifact
        assert not (state_root / "current").exists()
        assert not (state_root / "history.jsonl").exists()


class TestApplyWallpaperMutex:
    """D1 review decision: state read-modify-write is serialized."""

    def test_save_happens_inside_the_mutex_critical_section(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        events: list[str] = []
        repo = _FakeStateRepo(events=events)
        mutex = _FakeMutex(events=events)
        use_case = _make_use_case(tmp_path, repo, mutex=mutex)
        img = _img_in(tmp_path, "wall.png", b"mutex ordering bytes")

        use_case.run(img)

        # Fast-path load precedes the lock (fail-fast corrupt guard);
        # the authoritative reload + save happen strictly inside it.
        assert events == [
            "load",
            "acquire(blocking=True)",
            "load",
            "save",
            "release",
        ]
        assert mutex.holds == 1

    def test_derivation_happens_outside_the_lock(self, tmp_path: Path) -> None:
        """Tool invocations run before the mutex is acquired (stay parallel)."""
        _setup_spine(tmp_path / "install")
        events: list[str] = []
        repo = _FakeStateRepo(events=events)
        mutex = _FakeMutex(events=events)
        csg = _FakeCsg(events=events)
        use_case = _make_use_case(tmp_path, repo, csg=csg, mutex=mutex)
        img = _img_in(tmp_path, "wall.png", b"outside lock bytes")

        use_case.run(img)

        assert events.index("csg") < events.index("acquire(blocking=True)")
        assert events[-1] == "release"


class TestApplyWallpaperLostRenameRace:
    """Review P7: lost populate_via_staging race rebuilds from meta.json."""

    def test_lost_palette_race_returns_winner_entry_from_meta(self, tmp_path: Path) -> None:
        _setup_spine(tmp_path / "install")
        state_root = tmp_path / "state"
        repo = _FakeStateRepo()
        img = _img_in(tmp_path, "wall.png", b"race loser bytes")

        wh = hash_file(img)
        tsh = canonical_hash_dir(
            tmp_path / "install" / "config" / "color-scheme-generator" / "templates"
        )
        peh = palette_entry_hash(wh, tsh)
        target = state_root / "cache" / "palettes" / peh

        class _RaceWinnerCsg(_FakeCsg):
            """Honest fake that ALSO simulates the concurrent winner: it
            creates the final target entry (artifacts + meta.json) while
            the loser's staging populate is in flight."""

            def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
                entry = super().generate(wallpaper_path, output_dir)
                target.mkdir(parents=True, exist_ok=True)
                for name in (
                    "colors.yaml",
                    "colors.conf",
                    "colors.gtk.css",
                    "colors.adw.css",
                    "colors.sequences",
                    "colors.rasi",
                ):
                    (target / name).write_text("winner")
                (target / "meta.json").write_text(
                    json.dumps(
                        {
                            "hash_algorithm": "sha256",
                            "kind": "palette",
                            "entry_hash": peh,
                            "source_wallpaper_hash": wh,
                            "input_template_hash": tsh,
                            "artifact_hashes": {
                                name: hash_file(target / name)
                                for name in (
                                    "colors.yaml",
                                    "colors.conf",
                                    "colors.gtk.css",
                                    "colors.adw.css",
                                    "colors.sequences",
                                    "colors.rasi",
                                )
                            },
                            "generated_at": _now_z(),
                        }
                    )
                )
                return entry

        csg = _RaceWinnerCsg()
        use_case = _make_use_case(tmp_path, repo, csg=csg)

        result = use_case.run(img)

        assert csg.calls == 1  # invoked, but lost the rename
        assert result.cache_hit_palette  # reported as a hit: entry exists
        assert result.palette.entry_hash == peh
        assert repo.saved[0].palette is not None
        assert repo.saved[0].palette.entry_hash == peh  # real hashes, no sentinel


class TestSeederImportWallpaper:
    """Review D2 decision: cache owns its bytes + write-once meta backfill."""

    def _seeder(self, tmp_path: Path) -> Any:
        return CacheSeeder(tmp_path / "state")

    def test_copy_policy_owns_bytes_not_an_alias(self, tmp_path: Path) -> None:
        seeder = self._seeder(tmp_path)
        img = _img_in(tmp_path, "wall.png", b"user-owned bytes")
        wh = hash_file(img)

        dst = seeder.import_wallpaper(img, wh, source_mutable=True)

        assert dst.is_file()
        assert dst.read_bytes() == b"user-owned bytes"
        assert dst.stat().st_ino != img.stat().st_ino  # copy, not hardlink
        meta = json.loads(dst.parent.joinpath("meta.json").read_text())
        assert meta["content_hash"] == wh
        assert meta["source_path"] == str(img)

    def test_hardlink_policy_preserves_ad16(self, tmp_path: Path) -> None:
        seeder = self._seeder(tmp_path)
        img = _img_in(tmp_path, "wall.png", b"provisioning bytes")
        wh = hash_file(img)

        dst = seeder.import_wallpaper(img, wh, source_mutable=False)

        assert dst.stat().st_ino == img.stat().st_ino  # hardlink (AD-16)

    def test_preexisting_entry_with_wrong_content_raises(self, tmp_path: Path) -> None:
        seeder = self._seeder(tmp_path)
        img = _img_in(tmp_path, "wall.png", b"the real bytes")
        wh = hash_file(img)
        entry_dir = tmp_path / "state" / "cache" / "wallpapers" / wh
        entry_dir.mkdir(parents=True)
        (entry_dir / "wallpaper.png").write_bytes(b"corrupt content")

        with pytest.raises(RuntimeError, match="does not match its hash address"):
            seeder.import_wallpaper(img, wh, source_mutable=True)

    def test_source_mutated_between_hash_and_import_is_never_cached(self, tmp_path: Path) -> None:
        """TOCTOU guard: post-place verification removes the poisoned entry."""
        seeder = self._seeder(tmp_path)
        img = _img_in(tmp_path, "wall.png", b"original bytes")
        wh = hash_file(img)
        img.write_bytes(b"MUTATED in-place between hashing and import")  # same inode

        with pytest.raises(RuntimeError, match="does not match its hash address"):
            seeder.import_wallpaper(img, wh, source_mutable=True)

        assert not (
            tmp_path / "state" / "cache" / "wallpapers" / wh
        ).exists()  # poisoned entry removed, cache not silently wrong

    def test_missing_meta_json_is_backfilled(self, tmp_path: Path) -> None:
        """A prior crash mid-entry is repaired on the next import."""
        seeder = self._seeder(tmp_path)
        img = _img_in(tmp_path, "wall.png", b"crash survivor bytes")
        wh = hash_file(img)
        entry_dir = tmp_path / "state" / "cache" / "wallpapers" / wh
        entry_dir.mkdir(parents=True)
        (entry_dir / "wallpaper.png").write_bytes(b"crash survivor bytes")

        seeder.import_wallpaper(img, wh, source_mutable=True)

        meta = json.loads((entry_dir / "meta.json").read_text())
        assert meta["content_hash"] == wh

    def test_existing_meta_json_is_never_overwritten(self, tmp_path: Path) -> None:
        seeder = self._seeder(tmp_path)
        img = _img_in(tmp_path, "wall.png", b"write-once check bytes")
        wh = hash_file(img)
        entry_dir = tmp_path / "state" / "cache" / "wallpapers" / wh
        entry_dir.mkdir(parents=True)
        (entry_dir / "wallpaper.png").write_bytes(b"write-once check bytes")
        (entry_dir / "meta.json").write_text(
            json.dumps(
                {
                    "hash_algorithm": "sha256",
                    "kind": "wallpaper",
                    "content_hash": wh,
                    "source_path": "/original/first/import.png",
                    "imported_at": "2026-01-01T00:00:00Z",
                }
            )
        )

        seeder.import_wallpaper(img, wh, source_mutable=True)

        meta = json.loads((entry_dir / "meta.json").read_text())
        assert meta["source_path"] == "/original/first/import.png"
        assert meta["imported_at"] == "2026-01-01T00:00:00Z"
