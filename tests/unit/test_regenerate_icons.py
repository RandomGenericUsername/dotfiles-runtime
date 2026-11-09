"""Unit tests for ``RegenerateIconsUseCase``.

Icons-only regeneration from the live palette: fail-loud on absent/corrupt
state, icons-only repoint, ``trigger="regenerate"`` history with
``details.layers={icons: 1}``, AGS-only reload wiring, and the REQ-007
regression that the spine mappings are rendered verbatim (no retargeting).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.support.palette_fakes import v1_colors_yaml, v1_gtk_css

from runtime.adapters.flock_seed_mutex import PassThroughSeedMutex
from runtime.adapters.hashing import canonical_hash_dir, hash_file, icons_entry_hash
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.application.regenerate_icons import RegenerateIconsUseCase
from runtime.domain.models import (
    BackendType,
    DesktopState,
    FitMode,
    IconsArtifacts,
    IconsEntry,
    MonitorWallpaperConfig,
    PaletteArtifacts,
    PaletteEntry,
    WallpaperEntry,
)

LIGHT_PALETTE = (
    'background: "#f2f2f2"\n'
    'foreground: "#1a1a1a"\n'
    'cursor: "#1a1a1a"\n'
    "colors:\n"
    '  - "#000000"\n' + "".join('  - "#808080"\n' for _ in range(14)) + '  - "#ffffff"\n'
)

SPINE_ICONS = """battery:
  color_mappings:
    COLOR_FOREGROUND: color15
    COLOR_JOIN: color15
"""


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _FakeItr:
    """Contract-honest fake: ``ih`` recomputed from the path received."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_mappings: list[Path] = []

    def render(
        self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path
    ) -> IconsEntry:
        self.calls += 1
        self.seen_mappings.append(mappings_path)
        mappings_hash = (
            canonical_hash_dir(mappings_path)
            if mappings_path.is_dir()
            else hash_file(mappings_path)
        )
        templates_hash = canonical_hash_dir(templates_dir)
        ieh = icons_entry_hash(palette_hash, templates_hash, mappings_hash)
        assert output_dir.name == ieh
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=ieh,
            source_palette_hash=palette_hash,
            input_templates_hash=templates_hash,
            input_mappings_hash=mappings_hash,
            artifact_hashes=IconsArtifacts(**{"icon.svg": hash_file(output_dir / "icon.svg")}),
            generated_at=_now_z(),
        )


class _FakeReloader:
    def __init__(self, *, ok: bool = True) -> None:
        self.calls = 0
        self._ok = ok

    def reload(self) -> bool:
        self.calls += 1
        return self._ok


def _setup(tmp_path: Path) -> dict[str, Any]:
    """Live state (wallpaper + palette, null icons) + fakes + use case."""
    install_spine = tmp_path / "install"
    templates = install_spine / "icon-templates"
    templates.mkdir(parents=True)
    (templates / "terminal.svg").write_text("<svg/>")
    mappings_dir = install_spine / "icon-mappings"
    mappings_dir.mkdir(parents=True)
    mappings_file = mappings_dir / "icons.yaml"
    mappings_file.write_text(SPINE_ICONS)

    state_root = tmp_path / "state"
    img = tmp_path / "wall.png"
    img.write_bytes(b"live wallpaper bytes")
    wallpaper_hash = hash_file(img)

    seeder = CacheSeeder(state_root)
    peh = "1a" * 32
    palette_dir = state_root / "cache" / "palettes" / peh
    palette_dir.mkdir(parents=True)
    # A complete, conforming palette entry: the one convergence validates it
    # before committing the generation.
    (palette_dir / "colors.yaml").write_text(v1_colors_yaml())
    (palette_dir / "colors.conf").write_text("colors {}")
    (palette_dir / "colors.gtk.css").write_text(v1_gtk_css())
    (palette_dir / "colors.adw.css").write_text("colors {}")
    (palette_dir / "colors.sequences").write_bytes(b"\x1b]4;0;#000\x1b\\")
    (palette_dir / "colors.rasi").write_text("* { background: #000; }")
    (palette_dir / "colors.kitty").write_text("* { background: #000; }")
    (palette_dir / "colors.btop.theme").write_text("theme[colors.main_bg]=#000000\n")
    seeder.write_palette_meta_in(
        palette_dir,
        entry_hash=peh,
        source_wallpaper_hash=wallpaper_hash,
        input_template_hash="c" * 64,
        artifact_hashes={
            "colors.yaml": "0" * 64,
            "colors.conf": "0" * 64,
            "colors.gtk.css": "0" * 64,
            "colors.adw.css": "0" * 64,
            "colors.sequences": "0" * 64,
            "colors.rasi": "0" * 64,
            "colors.kitty": "0" * 64,
            "colors.btop.theme": "0" * 64,
            "colors.btop.theme": "0" * 64,
            "colors.btop.theme": "0" * 64,
        },
    )
    wall_dir = state_root / "cache" / "wallpapers" / wallpaper_hash
    wall_dir.mkdir(parents=True)
    (wall_dir / "wallpaper.png").write_bytes(b"live wallpaper bytes")

    now = _now_z()
    palette = PaletteEntry(
        hash_algorithm="sha256",
        kind="palette",
        entry_hash=peh,
        source_wallpaper_hash=wallpaper_hash,
        input_template_hash="c" * 64,
        artifact_hashes=PaletteArtifacts(
            colors_yaml="0" * 64,
            colors_conf="0" * 64,
            colors_gtk_css="0" * 64,
            colors_adw_css="0" * 64,
            colors_sequences="0" * 64,
            colors_rasi="0" * 64,
            colors_kitty="0" * 64,
        ),
        generated_at=now,
    )
    state = DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wallpaper_hash,
            source_path=str(img),
            imported_at=now,
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=wallpaper_hash,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=palette,
        effects=None,
        icons=None,
        applied_at=now,
    )
    repo = JsonStateRepository(state_root)
    repo.save(state)

    itr = _FakeItr()
    reloader = _FakeReloader()

    def _rebuild(
        reloaders: list[Any] | None = None,
    ) -> RegenerateIconsUseCase:
        return RegenerateIconsUseCase(
            state_repo=repo,
            palette_producer=None,  # type: ignore[arg-type]
            weg=None,  # type: ignore[arg-type]
            itr=itr,  # type: ignore[arg-type]
            install_spine=install_spine,
            state_root=state_root,
            seeder=seeder,
            mutex=PassThroughSeedMutex(),
            reloaders=[reloader] if reloaders is None else reloaders,  # type: ignore[list-item]
        )

    use_case = _rebuild()
    return {
        "use_case": use_case,
        "rebuild": _rebuild,
        "repo": repo,
        "seeder": seeder,
        "itr": itr,
        "reloader": reloader,
        "state_root": state_root,
        "install_spine": install_spine,
        "mappings_file": mappings_file,
        "wallpaper_hash": wallpaper_hash,
        "peh": peh,
    }


def _history_lines(state_root: Path) -> list[dict[str, Any]]:
    path = state_root / "history.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


class TestRegenerateIconsUseCase:
    def test_rederives_icons_only(self, tmp_path: Path) -> None:
        ctx = _setup(tmp_path)
        result = ctx["use_case"].run()

        assert result.cache_hit is False
        # REQ-007 regression: the spine mappings reach the renderer verbatim.
        assert ctx["itr"].seen_mappings[0] == ctx["mappings_file"]
        # v3: the use case records the new icons input; the next converge
        # commits a generation. It never writes a per-family current/ link.
        assert result.repointed == []
        assert result.consumer_symlinks == []
        # State carries the new icons entry.
        saved = ctx["repo"].load_current()
        assert saved is not None and saved.icons is not None
        assert saved.icons.entry_hash == result.icons.entry_hash
        # History: existing trigger + layers detail, nothing invented.
        lines = _history_lines(ctx["state_root"])
        assert len(lines) == 1
        assert lines[0]["trigger"] == "regenerate"
        assert lines[0]["details"] == {"layers": {"icons": 1}}
        assert lines[0]["icons"] == result.icons.entry_hash
        # Reload ran once (the CLI wires AGS only).
        assert ctx["reloader"].calls == 1
        assert result.reload_failures == []

    def test_regenerate_renders_spine_mappings_without_retargeting(
        self, tmp_path: Path
    ) -> None:
        """REQ-007 regression: the manifest mapping token is what resolves."""
        ctx = _setup(tmp_path)
        result = ctx["use_case"].run()

        assert ctx["itr"].seen_mappings == [ctx["mappings_file"]]
        assert result.icons.input_mappings_hash == hash_file(ctx["mappings_file"])
        assert not (ctx["state_root"] / "icon-contrast.json").exists()

    def test_second_run_is_cache_hit(self, tmp_path: Path) -> None:
        ctx = _setup(tmp_path)
        first = ctx["use_case"].run()
        second = ctx["use_case"].run()

        assert second.cache_hit is True
        assert second.icons.entry_hash == first.icons.entry_hash
        assert ctx["itr"].calls == 1

    def test_absent_state_fails_loud_without_mutation(self, tmp_path: Path) -> None:
        ctx = _setup(tmp_path)
        (ctx["state_root"] / "current.json").unlink()

        with pytest.raises(RuntimeError, match="nothing to regenerate"):
            ctx["use_case"].run()

        assert not (ctx["state_root"] / "history.jsonl").exists()
        assert _history_lines(ctx["state_root"]) == []

    def test_corrupt_state_fails_loud(self, tmp_path: Path) -> None:
        ctx = _setup(tmp_path)
        (ctx["state_root"] / "current.json").write_text("{corrupt", encoding="utf-8")

        with pytest.raises(ValueError, match="current.json"):
            ctx["use_case"].run()

    def test_null_palette_fails_loud(self, tmp_path: Path) -> None:
        import dataclasses

        ctx = _setup(tmp_path)
        state = ctx["repo"].load_current()
        assert state is not None
        ctx["repo"].save(dataclasses.replace(state, palette=None))

        with pytest.raises(RuntimeError, match="needs a palette"):
            ctx["use_case"].run()

    def test_reload_failure_reported_not_raised(self, tmp_path: Path) -> None:
        ctx = _setup(tmp_path)
        use_case = ctx["rebuild"](reloaders=[_FakeReloader(ok=False)])

        result = use_case.run()

        assert result.reload_failures == ["_FakeReloader"]
        # Convergence still persisted + recorded.
        assert _history_lines(ctx["state_root"])[0]["trigger"] == "regenerate"

    def test_concurrent_runs_serialize_on_the_one_lock(self, tmp_path: Path) -> None:
        from runtime.adapters.generation_store import GenerationStore

        ctx = _setup(tmp_path)
        errors: list[Exception] = []

        def _run() -> None:
            try:
                ctx["use_case"].run()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        state = ctx["repo"].load_current()
        assert state is not None and state.icons is not None
        lines = _history_lines(ctx["state_root"])
        assert lines and lines[-1]["trigger"] == "regenerate"
        assert lines[-1]["icons"] == state.icons.entry_hash
        manifest = GenerationStore(ctx["state_root"]).current_manifest()
        assert manifest is not None
        assert manifest["layers"]["icons"]["entryHash"] == state.icons.entry_hash
        assert not (ctx["state_root"] / ".seed.lock").exists()

    def test_icons_regenerate_and_wallpaper_converge_share_one_lock(
        self, tmp_path: Path
    ) -> None:
        """A concurrent icons regenerate + reconcile has exactly one writer.

        Both enter the same ``converge.lock``; whichever runs second sees the
        first committed result. The final history tail, ``current.json`` and
        ``current/manifest.json`` agree on the icons entry.
        """
        from runtime.adapters.generation_store import GenerationStore
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        ctx = _setup(tmp_path)
        reconcile = ReconcileDesktopStateUseCase(
            state_repo=ctx["repo"],
            palette_producer=None,  # type: ignore[arg-type]
            weg=None,  # type: ignore[arg-type]
            itr=ctx["itr"],  # type: ignore[arg-type]
            install_spine=ctx["install_spine"],
            state_root=ctx["state_root"],
            seeder=ctx["seeder"],
            mutex=PassThroughSeedMutex(),
            reloaders=[],
        )
        errors: list[Exception] = []
        start = threading.Barrier(2)

        def _icons() -> None:
            start.wait()
            try:
                ctx["use_case"].run()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def _converge() -> None:
            start.wait()
            try:
                reconcile.run(trigger="reconcile")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_icons), threading.Thread(target=_converge)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        state = ctx["repo"].load_current()
        assert state is not None and state.icons is not None
        lines = _history_lines(ctx["state_root"])
        assert lines and lines[-1]["icons"] == state.icons.entry_hash
        manifest = GenerationStore(ctx["state_root"]).current_manifest()
        assert manifest is not None
        assert manifest["layers"]["icons"]["entryHash"] == state.icons.entry_hash
        assert not (ctx["state_root"] / ".seed.lock").exists()
