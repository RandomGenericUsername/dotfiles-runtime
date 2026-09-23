"""Unit tests for the icon-contrast overlay in ``DerivationPipeline.ensure_icons`` (§3).

Covers tasks.md §3.5 (unit half): overlay content, scope guard (accent
variants / ``bar_mappings`` / literals / non-bar groups preserved),
deterministic bytes, effective-hash wiring, graceful fallback, config env
overrides, and the no-YAML-subset parsers. No real ``itr`` binary is used
(the fake renderer is contract-honest: it recomputes ``ih`` from the path
it actually received, so a wrong overlay path fails the test).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from runtime.adapters.hashing import canonical_hash_dir, hash_file, icons_entry_hash
from runtime.adapters.seeder import CacheSeeder
from runtime.application import derive as derive_mod
from runtime.application.derive import (
    DerivationPipeline,
    _parse_palette_mapping,
    _patch_overlay_text,
    _scan_group_color_mappings,
    icon_contrast_band_px,
    icon_contrast_groups,
    icon_contrast_threshold,
)
from runtime.domain.icon_contrast import BAR_GROUPS, DEFAULT_THRESHOLD
from runtime.domain.models import IconsEntry

PEH = "a" * 64
WH = "b" * 64

SPINE_ICONS = """# spine header comment — must survive the overlay
battery:
  color_mappings:
    COLOR_ACCENT: color13
    COLOR_FOREGROUND: color15 # trailing comment stays
    COLOR_JOIN: color15
  variants:
    - name: battery-0
      template: status-bar/battery/battery-0/default/icon.svg
      output: battery-0.svg
      color_mappings:
        COLOR_FOREGROUND: color15
  bar_mappings:
    widget: battery
    icon_size: 24
network:
  color_mappings:
    COLOR_FOREGROUND: color15
    COLOR_CROSS: color13
    COLOR_JOIN: "#aabbcc"
  bar_mappings:
    widget: network
capture-tool:
  color_mappings:
    COLOR_FOREGROUND: color15
  variants:
    - name: camera-accent
      template: x/camera.svg
      output: camera-accent.svg
      color_mappings:
        COLOR_FOREGROUND: color6
    - name: warning-caution
      template: x/warning.svg
      output: warning-caution.svg
      color_mappings:
        COLOR_FOREGROUND: color3
"""

SPINE_DEFAULTS = """defaults:
  COLOR_FOREGROUND: foreground
  COLOR_BACKGROUND: surface
  COLOR_SECONDARY: accent
  COLOR_ACCENT: accent-muted
"""

LIGHT_PALETTE = (
    'background: "#f2f2f2"\n'
    'foreground: "#1a1a1a"\n'
    'cursor: "#1a1a1a"\n'
    "colors:\n"
    '  - "#000000"\n' + "".join('  - "#808080"\n' for _ in range(14)) + '  - "#ffffff"\n'
)

DARK_PALETTE = (
    'background: "#101418"\n'
    'foreground: "#e8e8e8"\n'
    'cursor: "#e8e8e8"\n'
    "colors:\n"
    '  - "#000000"\n' + "".join('  - "#808080"\n' for _ in range(14)) + '  - "#ffffff"\n'
)


class _FakeItr:
    """Contract-honest fake: ``ih`` is recomputed from the path received."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_mappings: list[Path] = []
        self.seen_bytes: list[bytes] = []
        self.seen_hashes: list[str] = []
        self.seen_extra: bytes | None = None
        self.seen_vocab: bytes | None = None

    def render(
        self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path
    ) -> IconsEntry:
        from datetime import UTC, datetime

        from runtime.domain.models import IconsArtifacts

        self.calls += 1
        self.seen_mappings.append(mappings_path)
        if mappings_path.is_dir():
            target = mappings_path / "icons.yaml"
            self.seen_bytes.append(target.read_bytes())
            # Snapshot replicated siblings now — the staging overlay is
            # removed when ensure_icons exits.
            extra = mappings_path / "extra.yaml"
            self.seen_extra = extra.read_bytes() if extra.is_file() else None
            vocab = mappings_path / "defaults.yaml"
            self.seen_vocab = vocab.read_bytes() if vocab.is_file() else None
            mappings_hash = canonical_hash_dir(mappings_path)
        else:
            self.seen_bytes.append(mappings_path.read_bytes())
            # Snapshot the staged vocabulary copy (file-case overlay must
            # carry its sibling defaults.yaml or ITR renders with an empty
            # vocabulary). Hashing mirrors the pipeline AND the real
            # ItrAdapter: the received file alone (the adapter recomputes
            # the entry hash from exactly this path).
            vocab = mappings_path.parent / "defaults.yaml"
            if vocab.is_file():
                self.seen_vocab = vocab.read_bytes()
            else:
                self.seen_vocab = None
            mappings_hash = hash_file(mappings_path)
        templates_hash = canonical_hash_dir(templates_dir)
        self.seen_hashes.append(mappings_hash)
        ieh = icons_entry_hash(palette_hash, templates_hash, mappings_hash)
        assert output_dir.name == ieh, "pipeline ih must hash the EFFECTIVE mappings"
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
            generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )


def _setup(
    tmp_path: Path,
    *,
    palette_text: str = LIGHT_PALETTE,
    wallpaper_bytes: bytes = b"not a decodable image",
    mappings_text: str = SPINE_ICONS,
    mappings_dir: bool = False,
) -> tuple[DerivationPipeline, _FakeItr, Path, Path]:
    """Spine + palette/wallpaper cache + pipeline with a fake itr."""
    install_spine = tmp_path / "install"
    templates = install_spine / "icon-templates"
    templates.mkdir(parents=True)
    (templates / "terminal.svg").write_text("<svg/>")
    if mappings_dir:
        mappings = install_spine / "icon-mappings"
        mappings.mkdir(parents=True)
        (mappings / "icons.yaml").write_text(mappings_text)
        (mappings / "extra.yaml").write_text("extra: true\n")
        (mappings / "defaults.yaml").write_text(SPINE_DEFAULTS)
    else:
        mappings = install_spine / "icon-mappings"
        mappings.mkdir(parents=True)
        (mappings / "icons.yaml").write_text(mappings_text)
        # Sibling vocabulary, like the real spine: the file-case overlay
        # must stage a copy next to the patched icons.yaml or ITR renders
        # with an empty vocabulary (groups with empty color_mappings fail).
        (mappings / "defaults.yaml").write_text(SPINE_DEFAULTS)
        mappings = mappings / "icons.yaml"
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
    (wall_dir / "wallpaper.png").write_bytes(wallpaper_bytes)
    itr = _FakeItr()
    pipeline = DerivationPipeline(
        state_root=state_root,
        seeder=seeder,
        csg=None,  # type: ignore[arg-type]
        weg=None,  # type: ignore[arg-type]
        itr=itr,  # type: ignore[arg-type]
        install_spine=install_spine,
    )
    return pipeline, itr, state_root, mappings


def _meta(state_root: Path, entry_hash: str) -> dict:
    return json.loads((state_root / "cache" / "icons" / entry_hash / "meta.json").read_text())


class TestOverlayContent:
    def test_light_backdrop_flips_group_foreground(self, tmp_path: Path) -> None:
        pipeline, itr, state_root, _ = _setup(tmp_path)
        entry, hit = pipeline.ensure_icons(PEH)

        assert hit is False
        assert itr.calls == 1
        overlay_text = itr.seen_bytes[0].decode()
        assert "COLOR_FOREGROUND: color0 # trailing comment stays" in overlay_text
        assert "COLOR_JOIN: color0" in overlay_text
        assert "COLOR_ACCENT: color13" in overlay_text  # non-allowlisted placeholder kept
        assert "COLOR_CROSS: color13" in overlay_text

        meta = _meta(state_root, entry.entry_hash)
        assert meta["contrast"]["backdrop_source"] == "palette"
        assert meta["contrast"]["threshold"] == pytest.approx(DEFAULT_THRESHOLD)
        decisions = meta["contrast"]["decisions"]
        by_key = {(d["group"], d["placeholder"]): d for d in decisions}
        assert by_key[("battery", "COLOR_FOREGROUND")]["from"] == "color15"
        assert by_key[("battery", "COLOR_FOREGROUND")]["to"] == "color0"
        assert by_key[("battery", "COLOR_FOREGROUND")]["ratio_before"] < DEFAULT_THRESHOLD
        assert by_key[("battery", "COLOR_FOREGROUND")]["ratio_after"] >= DEFAULT_THRESHOLD
        assert ("network", "COLOR_FOREGROUND") in by_key

    def test_overlay_was_passed_not_spine(self, tmp_path: Path) -> None:
        pipeline, itr, _, mappings = _setup(tmp_path)
        pipeline.ensure_icons(PEH)

        assert itr.seen_mappings[0] != mappings
        assert itr.seen_mappings[0].name == "icons.yaml"

    def test_file_overlay_carries_vocab_sibling(self, tmp_path: Path) -> None:
        # Regression: ITR resolves its vocabulary as
        # ``yaml.parent / "defaults.yaml"`` — a bare overlay file renders
        # with an EMPTY vocabulary and groups with empty color_mappings
        # (wlogout, email-client, wallpaper-selector) fail the whole render.
        pipeline, itr, _, _ = _setup(tmp_path)
        pipeline.ensure_icons(PEH)

        assert itr.seen_vocab is not None
        assert itr.seen_vocab.decode() == SPINE_DEFAULTS

    def test_effective_hash_differs_from_spine_hash(self, tmp_path: Path) -> None:
        pipeline, _, state_root, _ = _setup(tmp_path)
        entry, _ = pipeline.ensure_icons(PEH)

        templates = tmp_path / "install" / "icon-templates"
        spine_hash = hash_file(tmp_path / "install" / "icon-mappings" / "icons.yaml")
        spine_ieh = icons_entry_hash(PEH, canonical_hash_dir(templates), spine_hash)
        assert entry.entry_hash != spine_ieh
        assert entry.input_mappings_hash != spine_hash


class TestScopeGuard:
    def test_variants_bar_mappings_literals_nonbar_preserved(self, tmp_path: Path) -> None:
        pipeline, itr, _, _ = _setup(tmp_path)
        pipeline.ensure_icons(PEH)

        overlay_text = itr.seen_bytes[0].decode()
        # variant-level entries (indent 6) untouched
        assert "      color_mappings:\n        COLOR_FOREGROUND: color15" in overlay_text
        assert "COLOR_FOREGROUND: color6" in overlay_text
        assert "COLOR_FOREGROUND: color3" in overlay_text
        # bar_mappings untouched
        assert "  bar_mappings:\n    widget: battery" in overlay_text
        assert "    icon_size: 24" in overlay_text
        # literal untouched
        assert 'COLOR_JOIN: "#aabbcc"' in overlay_text
        # non-bar group untouched
        capture_block = overlay_text.split("capture-tool:")[1].split("camera-accent")[0]
        assert "COLOR_FOREGROUND: color15" in capture_block
        # spine header comment preserved
        assert overlay_text.startswith("# spine header comment — must survive the overlay\n")

    def test_groups_env_narrows_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RUNTIME__ICON_CONTRAST__GROUPS", "battery")
        pipeline, itr, state_root, _ = _setup(tmp_path)
        entry, _ = pipeline.ensure_icons(PEH)

        overlay_text = itr.seen_bytes[0].decode()
        assert "COLOR_FOREGROUND: color0" in overlay_text  # battery flipped
        network_block = overlay_text.split("network:")[1].split("capture-tool:")[0]
        assert "COLOR_FOREGROUND: color15" in network_block  # network kept
        meta = _meta(state_root, entry.entry_hash)
        assert {d["group"] for d in meta["contrast"]["decisions"]} == {"battery"}

    def test_system_variant_pin_survives_light_group_flip(self, tmp_path: Path) -> None:
        """add-system-icon-variants: a light palette retargets the BARE
        ``volume`` group line, but ``system-*`` variant blocks (pin + output)
        stay byte-identical in the overlay — the same structural exemption
        proven for ``camera-accent`` above."""
        spine = (
            "volume:\n"
            "  color_mappings:\n"
            "    COLOR_FOREGROUND: foreground\n"
            "  variants:\n"
            "    - name: low\n"
            "      template: volume/low/default/icon.svg\n"
            "      output: volume-low.svg\n"
            "    - name: system-low\n"
            "      template: volume/low/default/icon.svg\n"
            "      output: volume-system-low.svg\n"
            "      color_mappings:\n"
            "        COLOR_FOREGROUND: foreground\n"
        )
        # Light background with a BRIGHT foreground — the wash-out case the
        # guard exists for (LIGHT_PALETTE's own dark foreground already
        # clears the threshold and would never retarget).
        light_bright_fg = LIGHT_PALETTE.replace(
            'foreground: "#1a1a1a"', 'foreground: "#f4f4f4"'
        )
        pipeline, itr, state_root, _ = _setup(
            tmp_path, palette_text=light_bright_fg, mappings_text=spine
        )
        entry, _ = pipeline.ensure_icons(PEH)

        overlay_text = itr.seen_bytes[0].decode()
        assert overlay_text != spine  # the guard DID write an overlay
        # bare group line flipped (indent-4 group-level entry only)
        assert "\n    COLOR_FOREGROUND: color0\n" in overlay_text
        # system-* variant block byte-identical: pin + output unchanged
        system_block = (
            "    - name: system-low\n"
            "      template: volume/low/default/icon.svg\n"
            "      output: volume-system-low.svg\n"
            "      color_mappings:\n"
            "        COLOR_FOREGROUND: foreground\n"
        )
        assert system_block in overlay_text
        # bare variant output also untouched (only the group line retargets)
        assert "      output: volume-low.svg\n" in overlay_text
        meta = _meta(state_root, entry.entry_hash)
        assert {(d["group"], d["placeholder"]) for d in meta["contrast"]["decisions"]} == {
            ("volume", "COLOR_FOREGROUND")
        }


class TestDeterminismAndNoop:
    def test_same_inputs_same_bytes_same_hash(self, tmp_path: Path) -> None:
        pipeline_a, itr_a, _, _ = _setup(tmp_path / "a")
        entry_a, _ = pipeline_a.ensure_icons(PEH)
        pipeline_b, itr_b, _, _ = _setup(tmp_path / "b")
        entry_b, _ = pipeline_b.ensure_icons(PEH)

        assert entry_a.entry_hash == entry_b.entry_hash
        assert itr_a.seen_bytes[0] == itr_b.seen_bytes[0]

    def test_dark_wallpaper_noop_uses_spine_path(self, tmp_path: Path) -> None:
        pipeline, itr, state_root, mappings = _setup(tmp_path, palette_text=DARK_PALETTE)
        entry, hit = pipeline.ensure_icons(PEH)

        assert hit is False
        assert itr.seen_mappings[0] == mappings  # spine passed through, no overlay
        templates = tmp_path / "install" / "icon-templates"
        spine_ieh = icons_entry_hash(PEH, canonical_hash_dir(templates), hash_file(mappings))
        assert entry.entry_hash == spine_ieh  # pre-guard key stable
        meta = _meta(state_root, entry.entry_hash)
        assert "contrast" not in meta  # no-churn entries stay byte-identical

    def test_cache_hit_second_run_invokes_no_itr(self, tmp_path: Path) -> None:
        pipeline, itr, _, _ = _setup(tmp_path)
        _, hit_first = pipeline.ensure_icons(PEH)
        _, hit_second = pipeline.ensure_icons(PEH)

        assert hit_first is False
        assert hit_second is True
        assert itr.calls == 1


class TestContrastOptOut:
    """Per-wallpaper opt-out (``icon-contrast-opt-out`` §2.1).

    ``contrast_enabled=False`` skips the guard entirely: the spine path
    renders (pre-guard key, no overlay) and the entry records
    ``contrast.policy.enabled == False``. An enabled retarget additionally
    records ``policy == {source, enabled: True}``; enabled no-ops and
    guard failures stay contrast-free (no churn).
    """

    def test_disabled_renders_spine_with_preguard_key(self, tmp_path: Path) -> None:
        pipeline, itr, state_root, mappings = _setup(tmp_path)  # light ⇒ guard WOULD fire
        entry, hit = pipeline.ensure_icons(PEH, contrast_enabled=False, contrast_source="store")

        assert hit is False
        assert itr.seen_mappings[0] == mappings  # spine passed through, no overlay
        templates = tmp_path / "install" / "icon-templates"
        spine_ieh = icons_entry_hash(PEH, canonical_hash_dir(templates), hash_file(mappings))
        assert entry.entry_hash == spine_ieh
        assert _meta(state_root, entry.entry_hash)["contrast"] == {
            "policy": {"source": "store", "enabled": False}
        }

    def test_disabled_records_flag_source(self, tmp_path: Path) -> None:
        pipeline, _, state_root, _ = _setup(tmp_path)
        entry, _ = pipeline.ensure_icons(PEH, contrast_enabled=False, contrast_source="flag")

        assert _meta(state_root, entry.entry_hash)["contrast"]["policy"] == {
            "source": "flag",
            "enabled": False,
        }

    def test_enabled_retarget_records_policy(self, tmp_path: Path) -> None:
        pipeline, _, state_root, _ = _setup(tmp_path)
        entry, _ = pipeline.ensure_icons(PEH)

        meta = _meta(state_root, entry.entry_hash)
        assert meta["contrast"]["policy"] == {"source": "default", "enabled": True}
        assert meta["contrast"]["decisions"], "guard must still retarget on a light palette"

    def test_invalid_source_fails_loud(self, tmp_path: Path) -> None:
        pipeline, _, _, _ = _setup(tmp_path)
        with pytest.raises(ValueError, match="invalid contrast source"):
            pipeline.ensure_icons(PEH, contrast_source="registry")


class TestFallback:
    def test_guard_exception_warns_and_uses_spine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom(*args: object, **kwargs: object) -> dict:
            raise RuntimeError("domain exploded")

        monkeypatch.setattr(derive_mod, "decide_overrides", _boom)
        pipeline, itr, state_root, mappings = _setup(tmp_path)

        with caplog.at_level(logging.WARNING, logger="runtime.application.derive"):
            entry, _ = pipeline.ensure_icons(PEH)

        assert itr.seen_mappings[0] == mappings
        assert any("contrast guard failed" in r.getMessage() for r in caplog.records)
        meta = _meta(state_root, entry.entry_hash)
        assert "contrast" not in meta

    def test_missing_palette_cache_passes_spine_through(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        templates = install_spine / "icon-templates"
        templates.mkdir(parents=True)
        (templates / "terminal.svg").write_text("<svg/>")
        mappings_dir = install_spine / "icon-mappings"
        mappings_dir.mkdir(parents=True)
        mappings = mappings_dir / "icons.yaml"
        mappings.write_text(SPINE_ICONS)
        state_root = tmp_path / "state"
        itr = _FakeItr()
        pipeline = DerivationPipeline(
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            csg=None,  # type: ignore[arg-type]
            weg=None,  # type: ignore[arg-type]
            itr=itr,  # type: ignore[arg-type]
            install_spine=install_spine,
        )
        entry, _ = pipeline.ensure_icons("d" * 64)

        assert itr.seen_mappings[0] == mappings
        assert entry.entry_hash == icons_entry_hash(
            "d" * 64, canonical_hash_dir(templates), hash_file(mappings)
        )


class TestMappingsDir:
    def test_dir_replicated_with_only_icons_yaml_patched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NOTE: spine discovery prefers ``icon-mappings/icons.yaml`` (file)
        # over the dir, so the dir case is forced by pointing require_input
        # at a mappings dir — the overlay path must still work end to end.
        pipeline, itr, _, _ = _setup(tmp_path)
        mappings_dir = tmp_path / "mappings-dir"
        mappings_dir.mkdir()
        (mappings_dir / "icons.yaml").write_text(SPINE_ICONS)
        (mappings_dir / "extra.yaml").write_text("extra: true\n")
        (mappings_dir / "defaults.yaml").write_text(SPINE_DEFAULTS)
        templates_dir = tmp_path / "install" / "icon-templates"

        def _fake_require(key: str, _spine: Path) -> Path:
            return templates_dir if key == "icon_templates" else mappings_dir

        monkeypatch.setattr(derive_mod, "require_input", _fake_require)
        entry, _ = pipeline.ensure_icons(PEH)

        overlay_dir = itr.seen_mappings[0]
        assert overlay_dir.name != "mappings-dir"  # staging overlay, not the spine dir
        assert not overlay_dir.exists()  # staging cleaned up after ensure_icons
        assert itr.seen_extra == b"extra: true\n"  # dir replicated alongside
        patched = itr.seen_bytes[0].decode()
        assert "COLOR_FOREGROUND: color0" in patched
        assert "COLOR_FOREGROUND: color6" in patched  # variant kept
        assert entry.input_mappings_hash == itr.seen_hashes[0]  # effective hash wired


class TestSampledBackdrop:
    def test_decodable_wallpaper_records_sampled(self, tmp_path: Path) -> None:
        pil_image = pytest.importorskip("PIL.Image")
        img = pil_image.new("RGB", (64, 64), (250, 250, 250))
        from io import BytesIO

        buf = BytesIO()
        img.save(buf, format="PNG")
        pipeline, _, state_root, _ = _setup(tmp_path, wallpaper_bytes=buf.getvalue())
        entry, _ = pipeline.ensure_icons(PEH)

        meta = _meta(state_root, entry.entry_hash)
        assert meta["contrast"]["backdrop_source"] == "sampled"


class TestConfigEnv:
    def test_threshold_defaults_and_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RUNTIME__ICON_CONTRAST__THRESHOLD", raising=False)
        assert icon_contrast_threshold() == DEFAULT_THRESHOLD
        monkeypatch.setenv("RUNTIME__ICON_CONTRAST__THRESHOLD", "7.0")
        assert icon_contrast_threshold() == 7.0
        monkeypatch.setenv("RUNTIME__ICON_CONTRAST__THRESHOLD", "bogus")
        assert icon_contrast_threshold() == DEFAULT_THRESHOLD
        monkeypatch.setenv("RUNTIME__ICON_CONTRAST__THRESHOLD", "99")
        assert icon_contrast_threshold() == DEFAULT_THRESHOLD

    def test_groups_default_and_parse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RUNTIME__ICON_CONTRAST__GROUPS", raising=False)
        assert icon_contrast_groups() == BAR_GROUPS
        monkeypatch.setenv("RUNTIME__ICON_CONTRAST__GROUPS", "battery, network")
        assert icon_contrast_groups() == ("battery", "network")
        monkeypatch.setenv("RUNTIME__ICON_CONTRAST__GROUPS", "   ")
        assert icon_contrast_groups() == BAR_GROUPS

    def test_band_px_default_and_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RUNTIME__ICON_CONTRAST__BAND_PX", raising=False)
        assert icon_contrast_band_px() == 48
        monkeypatch.setenv("RUNTIME__ICON_CONTRAST__BAND_PX", "24")
        assert icon_contrast_band_px() == 24
        monkeypatch.setenv("RUNTIME__ICON_CONTRAST__BAND_PX", "0")
        assert icon_contrast_band_px() == 48
        monkeypatch.setenv("RUNTIME__ICON_CONTRAST__BAND_PX", "bogus")
        assert icon_contrast_band_px() == 48


class TestSubsetParsers:
    def test_palette_parser_shapes(self) -> None:
        assert _parse_palette_mapping(LIGHT_PALETTE)["background"] == "#f2f2f2"
        assert _parse_palette_mapping(LIGHT_PALETTE)["color0"] == "#000000"
        assert _parse_palette_mapping(LIGHT_PALETTE)["color15"] == "#ffffff"
        legacy = "special:\n  background: '#111111'\n  foreground: '#eeeeee'\n"
        assert _parse_palette_mapping(legacy)["background"] == "#111111"
        mapping_colors = "background: '#222222'\ncolors:\n  color0: '#000000'\n"
        assert _parse_palette_mapping(mapping_colors)["color0"] == "#000000"
        passthrough = "background: '#222222'\nsurface: '#333333'\n"
        assert _parse_palette_mapping(passthrough)["surface"] == "#333333"

    def test_scan_and_patch_roundtrip(self) -> None:
        scanned = _scan_group_color_mappings(SPINE_ICONS)
        assert scanned["battery"]["COLOR_FOREGROUND"] == "color15"
        assert scanned["battery"]["COLOR_JOIN"] == "color15"
        assert scanned["network"]["COLOR_JOIN"] == "#aabbcc"
        assert scanned["capture-tool"]["COLOR_FOREGROUND"] == "color15"
        # only allowlisted placeholders are collected (ACCENT/CROSS never enter scope)
        assert set(scanned["battery"]) == {"COLOR_FOREGROUND", "COLOR_JOIN"}

        patched = _patch_overlay_text(SPINE_ICONS, {("battery", "COLOR_FOREGROUND"): "color0"})
        assert "COLOR_FOREGROUND: color0 # trailing comment stays" in patched
        assert "COLOR_JOIN: color15" in patched  # sibling untouched
        assert "        COLOR_FOREGROUND: color15" in patched  # variant untouched
        # patch is idempotent in shape: re-scan finds the new token
        assert _scan_group_color_mappings(patched)["battery"]["COLOR_FOREGROUND"] == "color0"
