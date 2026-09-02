"""Integration test for the terminal palette applier (Story 2.6).

Honest contract: no real terminal exists in the dev/test host to recolor,
and the use case is composed DIRECTLY (``_make_reconcile``-style helper —
real ``JsonStateRepository`` + fake csg/weg/itr + fake mutex, mirroring
``test_hyprpaper_reloader_integration.py:145-155``), not through the CLI
composition root. The adapter writes to an injected tmp-file ``tty_path``
sink; the production default (``tty_path=None`` → ``/dev/tty``) is what
keeps the adapter honest, and the test injects the sink. The expected
payload is computed INDEPENDENTLY by re-parsing the seeded palette's
``colors.yaml`` from the cache entry (never by calling the adapter).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.adapters.terminal_color_applier import TerminalColorApplier
from runtime.application.reconcile import ReconcileDesktopStateUseCase
from runtime.domain.models import PaletteArtifacts, PaletteEntry


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _pinned_colors_yaml() -> str:
    """The pinned ``colors.yaml.j2`` schema, as the runtime's CSG renders it."""
    lines = [
        'background: "#1a1b26"',
        'foreground: "#c0caf5"',
        'cursor: "#c0caf5"',
        "colors:",
        *(f'  - "#{i:02x}{i:02x}{i:02x}"' for i in range(16)),
        'source_image: "seeded"',
        'backend: "fast"',
        'generated_at: "2026-09-02T00:00:00Z"',
    ]
    return "\n".join(lines) + "\n"


def _expected_payload_from_colors_yaml(path: Path) -> bytes:
    """Re-derive the OSC payload from a colors.yaml INDEPENDENT of the adapter."""
    text = path.read_text(encoding="utf-8")
    scalars = dict(re.findall(r'^(background|foreground|cursor):\s*"([^"]+)"', text, re.M))
    colors = re.findall(r'^  - "([^"]+)"', text, re.M)
    assert len(colors) == 16, "seeded palette must contain 16 colors"
    parts = [f"\x1b]4;{index};{color}\x1b\\" for index, color in enumerate(colors)]
    parts.append(f"\x1b]10;{scalars['foreground']}\x1b\\")
    parts.append(f"\x1b]11;{scalars['background']}\x1b\\")
    parts.append(f"\x1b]12;{scalars['cursor']}\x1b\\")
    return "".join(parts).encode("ascii")


class _FakeMutex:
    def hold(self, blocking: bool = False) -> object:  # type: ignore[no-untyped-def]
        class _Hold:
            def __enter__(self_inner) -> None:
                return None

            def __exit__(self_inner, *exc: object) -> None:
                pass

        return _Hold()


class _FakeCsg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "colors.yaml").write_text(_pinned_colors_yaml())
        (output_dir / "colors.conf").write_text(
            "background: #1a1b26\nforeground: #c0caf5\n"
        )
        (output_dir / "colors.gtk.css").write_text("/* palette */")
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
    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

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
    def render(
        self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path
    ) -> object:
        from runtime.domain.models import IconsArtifacts, IconsEntry

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
    csg_templates = install_spine / "config" / "color-scheme-generator" / "templates"
    csg_templates.mkdir(parents=True)
    (csg_templates / "default.yaml").write_text("window: {}\n")
    weg_cfg = install_spine / "config" / "weg"
    weg_cfg.mkdir(parents=True)
    (weg_cfg / "effects.yaml").write_text("effects: []\n")
    itr_templates = install_spine / "config" / "icon-templates-renderer" / "templates"
    itr_templates.mkdir(parents=True)
    (itr_templates / "terminal.svg").write_text("<svg/>")
    (install_spine / "config" / "icon-templates-renderer" / "icons.yaml").write_text("icons: {}\n")


def _make_reconcile(repo: object, state_root: Path, install_spine: Path, reloaders: list) -> object:
    return ReconcileDesktopStateUseCase(
        state_repo=repo,  # type: ignore[arg-type]
        csg=_FakeCsg(),  # type: ignore[arg-type]
        weg=_FakeWeg(),  # type: ignore[arg-type]
        itr=_FakeItr(),  # type: ignore[arg-type]
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),  # type: ignore[arg-type]
        reloaders=reloaders,  # type: ignore[arg-type]
    )


def _apply_wallpaper(tmp_path: Path, install_spine: Path, state_root: Path) -> None:
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    repo = JsonStateRepository(state_root=state_root)
    img = tmp_path / "wall.png"
    img.write_bytes(b"e2e wallpaper for terminal palette applier")
    ApplyWallpaperUseCase(
        state_repo=repo,
        csg=_FakeCsg(),  # type: ignore[arg-type]
        weg=_FakeWeg(),  # type: ignore[arg-type]
        itr=_FakeItr(),  # type: ignore[arg-type]
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),  # type: ignore[arg-type]
    ).run(img)


def _seeded_palette_yaml(state_root: Path) -> Path:
    yamls = sorted((state_root / "cache" / "palettes").glob("*/colors.yaml"))
    assert yamls, "seeded palette colors.yaml must exist in the cache entry"
    return yamls[0]


def test_terminal_palette_applier_integration_applies_seeded_palette(
    tmp_path: Path,
) -> None:
    """E2E: seed → apply → reconcile with the applier writing to a byte sink."""
    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    _apply_wallpaper(tmp_path, install_spine, state_root)

    palette_yaml = _seeded_palette_yaml(state_root)
    expected = _expected_payload_from_colors_yaml(palette_yaml)

    sink = tmp_path / "tty-sink"
    reloader = TerminalColorApplier(state_root=state_root, tty_path=sink)
    repo = JsonStateRepository(state_root=state_root)
    use_case = _make_reconcile(repo, state_root, install_spine, [reloader])
    result = use_case.run()  # type: ignore[attr-defined]
    assert result.reload_failures == []
    assert sink.read_bytes() == expected


def test_terminal_palette_applier_integration_unwritable_sink_is_surfaced(
    tmp_path: Path,
) -> None:
    """An unwritable apply target surfaces ``TerminalColorApplier`` (R5), not a skip.

    The CLI already exits non-zero when ``result.reload_failures`` is
    populated (cli/main.py reload-failure path); this test pins the
    surfaced-failure contract at the use-case boundary, mirroring
    ``test_hyprpaper_reloader_integration``'s assertions.
    """
    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    _apply_wallpaper(tmp_path, install_spine, state_root)

    unwritable = tmp_path / "is-a-directory"
    unwritable.mkdir()
    reloader = TerminalColorApplier(state_root=state_root, tty_path=unwritable)
    repo = JsonStateRepository(state_root=state_root)
    use_case = _make_reconcile(repo, state_root, install_spine, [reloader])
    result = use_case.run()  # type: ignore[attr-defined]
    assert "TerminalColorApplier" in result.reload_failures


def test_terminal_palette_applier_integration_vacuous_state(tmp_path: Path) -> None:
    """No ``current/colors.yaml`` to apply → vacuous True, sink untouched.

    Exercised against a real apply-seeded ``state_root`` whose ``current/``
    directory never materialized (``ApplyWallpaperUseCase`` does not repoint
    ``current/``): there is nothing to apply, so the adapter must succeed
    without touching the TTY and without producing a failure (the reconcile
    loop only sees a failure when ``reload()`` returns ``False``).
    """
    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    _apply_wallpaper(tmp_path, install_spine, state_root)

    sink = tmp_path / "tty-sink"
    reloader = TerminalColorApplier(state_root=state_root, tty_path=sink)
    assert reloader.reload() is True
    assert not sink.exists()