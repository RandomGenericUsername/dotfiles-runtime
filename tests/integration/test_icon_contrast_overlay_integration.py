"""Integration tests for the icon-contrast overlay (§3.5, integration half).

Drives ``DerivationPipeline.ensure_icons`` on a real filesystem (real
``CacheSeeder`` + spine, fake contract-honest itr that performs a minimal
placeholder substitution like the real renderer): a light wallpaper flips
bright bar icons to a dark token (rendered SVGs carry the picked hex, no
``{{`` remains), a dark wallpaper is a spine-identical no-op, a corrupt
image degrades via the palette fallback, and a repeat run is a cache hit
with zero itr invocations.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest

from runtime.adapters.hashing import canonical_hash_dir, hash_file, icons_entry_hash
from runtime.adapters.seeder import CacheSeeder
from runtime.application.derive import DerivationPipeline, _parse_palette_mapping
from runtime.domain.models import IconsEntry

pytestmark = pytest.mark.integration

PEH = "a" * 64
WH = "b" * 64

SPINE_ICONS = """battery:
  color_mappings:
    COLOR_FOREGROUND: color15
  variants:
    - name: battery-0
      template: status-bar/battery/battery-0/default/icon.svg
      output: battery-0.svg
  bar_mappings:
    widget: battery
"""

PALETTE_TEXT = (
    'background: "#f2f2f2"\n'
    'foreground: "#1a1a1a"\n'
    'cursor: "#1a1a1a"\n'
    "colors:\n"
    '  - "#000000"\n' + "".join('  - "#808080"\n' for _ in range(14)) + '  - "#ffffff"\n'
)

DARK_PALETTE_TEXT = PALETTE_TEXT.replace('"#f2f2f2"', '"#101418"')


def _png(color: tuple[int, int, int]) -> bytes:
    pil_image = pytest.importorskip("PIL.Image")
    img = pil_image.new("RGB", (64, 64), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _SubstitutingItr:
    """Fake itr with a minimal substitution pass over the EFFECTIVE mappings."""

    def __init__(self) -> None:
        self.calls = 0

    def render(
        self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path
    ) -> IconsEntry:
        from runtime.domain.models import IconsArtifacts

        self.calls += 1
        mappings_file = mappings_path / "icons.yaml" if mappings_path.is_dir() else mappings_path
        raw = mappings_file.read_text(encoding="utf-8")
        match = re.search(r"^    COLOR_FOREGROUND:[ \t]+(\S+)", raw, re.MULTILINE)
        assert match is not None
        token = match.group(1).strip("\"'")
        # work dir is cache/.staging-*/<ieh> during populate → cache is two levels up.
        palette_path = output_dir.parent.parent / "palettes" / palette_hash / "colors.yaml"
        palette = _parse_palette_mapping(palette_path.read_text(encoding="utf-8"))
        assert token in palette, f"overlay token {token!r} must be palette-resident"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "battery-0.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg"><path fill="{palette[token]}"/></svg>'
        )
        if mappings_path.is_dir():
            mappings_hash = canonical_hash_dir(mappings_path)
        else:
            # The real ItrAdapter recomputes the entry hash from exactly the
            # path it receives, so the fake hashes the file alone — same as
            # the pipeline.
            mappings_hash = hash_file(mappings_path)
        templates_hash = canonical_hash_dir(templates_dir)
        ieh = icons_entry_hash(palette_hash, templates_hash, mappings_hash)
        assert output_dir.name == ieh
        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=ieh,
            source_palette_hash=palette_hash,
            input_templates_hash=templates_hash,
            input_mappings_hash=mappings_hash,
            artifact_hashes=IconsArtifacts(
                **{"battery-0.svg": hash_file(output_dir / "battery-0.svg")}
            ),
            generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )


def _setup(
    tmp_path: Path, *, palette_text: str = PALETTE_TEXT, wallpaper_bytes: bytes | None = None
) -> tuple[DerivationPipeline, _SubstitutingItr, Path]:
    install_spine = tmp_path / "install"
    templates = install_spine / "icon-templates"
    templates.mkdir(parents=True)
    (templates / "terminal.svg").write_text("<svg/>")
    mappings_dir = install_spine / "icon-mappings"
    mappings_dir.mkdir(parents=True)
    (mappings_dir / "icons.yaml").write_text(SPINE_ICONS)
    # Sibling vocabulary, like the real spine: the file-case overlay must
    # stage a copy next to the patched icons.yaml or ITR renders with an
    # empty vocabulary (groups with empty color_mappings fail).
    (mappings_dir / "defaults.yaml").write_text("defaults:\n  COLOR_FOREGROUND: foreground\n")
    state_root = tmp_path / "state"
    seeder = CacheSeeder(state_root)
    palette_dir = state_root / "cache" / "palettes" / PEH
    palette_dir.mkdir(parents=True)
    (palette_dir / "colors.yaml").write_text(palette_text)
    seeder.write_palette_meta_in(
        palette_dir,
        entry_hash=PEH,
        source_wallpaper_hash=WH,
        input_template_hash="c" * 64,
        artifact_hashes={
            "colors.yaml": "0" * 64,
            "colors.conf": "0" * 64,
            "colors.gtk.css": "0" * 64,
            "colors.adw.css": "0" * 64,
            "colors.sequences": "0" * 64,
            "colors.rasi": "0" * 64,
            "colors.kitty": "0" * 64,
        },
    )
    wall_dir = state_root / "cache" / "wallpapers" / WH
    wall_dir.mkdir(parents=True)
    (wall_dir / "wallpaper.png").write_bytes(
        wallpaper_bytes if wallpaper_bytes is not None else _png((250, 250, 250))
    )
    itr = _SubstitutingItr()
    pipeline = DerivationPipeline(
        state_root=state_root,
        seeder=seeder,
        csg=None,  # type: ignore[arg-type]
        weg=None,  # type: ignore[arg-type]
        itr=itr,  # type: ignore[arg-type]
        install_spine=install_spine,
    )
    return pipeline, itr, state_root


def _meta(state_root: Path, entry_hash: str) -> dict:
    return json.loads((state_root / "cache" / "icons" / entry_hash / "meta.json").read_text())


class TestLightFlipEndToEnd:
    def test_light_wallpaper_flips_and_svg_carries_picked_hex(self, tmp_path: Path) -> None:
        pipeline, itr, state_root = _setup(tmp_path)
        entry, hit = pipeline.ensure_icons(PEH)

        assert hit is False
        assert itr.calls == 1
        svg = (state_root / "cache" / "icons" / entry.entry_hash / "battery-0.svg").read_text()
        assert 'fill="#000000"' in svg
        assert "{{" not in svg
        meta = _meta(state_root, entry.entry_hash)
        assert meta["contrast"]["backdrop_source"] == "sampled"
        decision = meta["contrast"]["decisions"][0]
        assert (decision["group"], decision["placeholder"]) == ("battery", "COLOR_FOREGROUND")
        assert (decision["from"], decision["to"]) == ("color15", "color0")

    def test_repeat_run_is_cache_hit_with_zero_itr(self, tmp_path: Path) -> None:
        pipeline, itr, _ = _setup(tmp_path)
        first, _ = pipeline.ensure_icons(PEH)
        second, hit = pipeline.ensure_icons(PEH)

        assert hit is True
        assert itr.calls == 1
        assert second.entry_hash == first.entry_hash


class TestDarkNoop:
    def test_dark_wallpaper_matches_preguard_entry(self, tmp_path: Path) -> None:
        pipeline, itr, state_root = _setup(
            tmp_path,
            palette_text=DARK_PALETTE_TEXT,
            wallpaper_bytes=_png((5, 5, 8)),
        )
        entry, _ = pipeline.ensure_icons(PEH)

        assert itr.calls == 1
        templates = tmp_path / "install" / "icon-templates"
        spine_ieh = icons_entry_hash(
            PEH,
            canonical_hash_dir(templates),
            hash_file(tmp_path / "install" / "icon-mappings" / "icons.yaml"),
        )
        assert entry.entry_hash == spine_ieh
        meta = _meta(state_root, entry.entry_hash)
        assert "contrast" not in meta
        svg = (state_root / "cache" / "icons" / entry.entry_hash / "battery-0.svg").read_text()
        assert 'fill="#ffffff"' in svg  # color15 kept


class TestCorruptDegrades:
    def test_corrupt_image_falls_back_to_palette(self, tmp_path: Path) -> None:
        pipeline, itr, state_root = _setup(tmp_path, wallpaper_bytes=b"corrupt bytes here")
        entry, hit = pipeline.ensure_icons(PEH)

        assert hit is False
        assert itr.calls == 1
        meta = _meta(state_root, entry.entry_hash)
        assert meta["contrast"]["backdrop_source"] == "palette"
        svg = (state_root / "cache" / "icons" / entry.entry_hash / "battery-0.svg").read_text()
        assert 'fill="#000000"' in svg


class TestOptOutPassthrough:
    """Per-wallpaper opt-out on a real filesystem (§2.1, integration half).

    A light wallpaper whose guard WOULD fire renders spine-identical
    output under ``contrast_enabled=False`` (pre-guard key, authored
    token in the SVG) and records ``contrast.policy.enabled == False``.
    """

    def test_disabled_renders_spine_token_with_policy(self, tmp_path: Path) -> None:
        pipeline, itr, state_root = _setup(tmp_path)
        entry, hit = pipeline.ensure_icons(
            PEH, contrast_enabled=False, contrast_source="store"
        )

        assert hit is False
        assert itr.calls == 1
        templates = tmp_path / "install" / "icon-templates"
        spine_ieh = icons_entry_hash(
            PEH,
            canonical_hash_dir(templates),
            hash_file(tmp_path / "install" / "icon-mappings" / "icons.yaml"),
        )
        assert entry.entry_hash == spine_ieh
        svg = (state_root / "cache" / "icons" / entry.entry_hash / "battery-0.svg").read_text()
        assert 'fill="#ffffff"' in svg  # authored color15 kept, not the guard's color0
        assert _meta(state_root, entry.entry_hash)["contrast"] == {
            "policy": {"source": "store", "enabled": False}
        }

    def test_enabled_retarget_records_policy(self, tmp_path: Path) -> None:
        pipeline, _, state_root = _setup(tmp_path)
        entry, _ = pipeline.ensure_icons(PEH, contrast_source="flag")

        meta = _meta(state_root, entry.entry_hash)
        assert meta["contrast"]["policy"] == {"source": "flag", "enabled": True}
        assert meta["contrast"]["decisions"]
