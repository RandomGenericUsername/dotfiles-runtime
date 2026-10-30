"""REQ-006 — ``palette.v1`` vocabulary contract, conformance, probe tests.

The probe test pins the declared role constant against the vendored
``tests/fixtures/palette-vocabulary-v1.json`` generated from theme-producer
``v0.5.0`` REQ-022 (50 Material roles + ``on_backdrop``). Deleting or renaming
a role must fail this suite.

Conformance fixtures (``tests/fixtures/palettes/``):

- ``v1-valid`` — all 51 declared roles in both named-token artifacts, plus
  legitimate extras (``foreground``, ``accent``, ANSI ``colors:`` list).
- ``v1-missing-role`` — ``primary`` removed from both artifacts.
- ``v1-extra-roles`` — valid + an additive ``experimental_surface`` role.
- ``v0.4.0`` — the real tpr pre-REQ-022 shape (no ``on_backdrop``); this must
  fail naming ``on_backdrop`` — the expected pre-pin behavior.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.adapters.hashing import hash_file
from runtime.adapters.seeder import CacheSeeder
from runtime.application.derive import DerivationPipeline
from runtime.application.doctor import DoctorUseCase
from runtime.application.palette_conformance import (
    check_palette_artifacts,
    check_palette_entry,
    require_palette_conformance,
)
from runtime.domain.models import PaletteEntry
from runtime.domain.palette_vocabulary import (
    PALETTE_VOCABULARY_ROLES,
    PALETTE_VOCABULARY_V1,
    PALETTE_VOCABULARY_VERSION,
    PaletteVocabularyError,
)
from tests.support.palette_fakes import PaletteProducerStub

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PALETTES = FIXTURES / "palettes"
VOCABULARY_FIXTURE = FIXTURES / "palette-vocabulary-v1.json"

_OTHER_ARTIFACTS = (
    "colors.conf",
    "colors.adw.css",
    "colors.sequences",
    "colors.rasi",
    "colors.kitty",
    "colors.btop.theme",
)


def _fixture(name: str) -> Path:
    return PALETTES / name


class _TprPaletteStub(PaletteProducerStub):
    """Contract-honest tpr fake: copies a fixture's two named-token files."""

    producer_name = "tpr"

    def __init__(self, fixture_dir: Path) -> None:
        self.fixture_dir = fixture_dir
        self.templates_dir = None

    def generate(self, wallpaper_path: Path, output_dir: Path) -> PaletteEntry:
        output_dir.mkdir(parents=True, exist_ok=True)
        for name in ("colors.yaml", "colors.gtk.css"):
            shutil.copyfile(self.fixture_dir / name, output_dir / name)
        for name in _OTHER_ARTIFACTS:
            (output_dir / name).write_text("placeholder", encoding="utf-8")
        artifact_hashes = {
            "colors_yaml": hash_file(output_dir / "colors.yaml"),
            "colors_conf": hash_file(output_dir / "colors.conf"),
            "colors_gtk_css": hash_file(output_dir / "colors.gtk.css"),
            "colors_adw_css": hash_file(output_dir / "colors.adw.css"),
            "colors_sequences": hash_file(output_dir / "colors.sequences"),
            "colors_rasi": hash_file(output_dir / "colors.rasi"),
            "colors_kitty": hash_file(output_dir / "colors.kitty"),
            "colors_btop": hash_file(output_dir / "colors.btop.theme"),
        }
        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=output_dir.name,
            source_wallpaper_hash="ab" * 32,
            input_template_hash=self.template_set_hash(),
            artifact_hashes=artifact_hashes,
            generated_at="2026-01-01T00:00:00Z",
        )


def _pipeline(tmp_path: Path, stub: PaletteProducerStub) -> DerivationPipeline:
    return DerivationPipeline(
        state_root=tmp_path,
        seeder=CacheSeeder(tmp_path),
        csg=stub,  # type: ignore[arg-type]
        weg=None,  # type: ignore[arg-type]
        itr=None,  # type: ignore[arg-type]
        install_spine=tmp_path,
    )


# ---------------------------------------------------------------------------
# Probe test — declared vocabulary vs vendored fixture
# ---------------------------------------------------------------------------


def test_declared_vocabulary_matches_probe_fixture() -> None:
    payload = json.loads(VOCABULARY_FIXTURE.read_text(encoding="utf-8"))
    assert payload["version"] == PALETTE_VOCABULARY_VERSION == "1"
    assert tuple(payload["roles"]) == PALETTE_VOCABULARY_V1
    assert "v0.5.0" in payload["source"]
    assert "REQ-022" in payload["source"]


def test_declared_vocabulary_is_the_50_material_roles_plus_on_backdrop() -> None:
    assert len(PALETTE_VOCABULARY_V1) == 51
    assert len(PALETTE_VOCABULARY_ROLES) == 51
    assert len(set(PALETTE_VOCABULARY_V1)) == 51
    assert PALETTE_VOCABULARY_ROLES == frozenset(PALETTE_VOCABULARY_V1)
    assert PALETTE_VOCABULARY_V1[-1] == "on_backdrop"
    # The source is theme-producer's MaterialRole list; spot-check both ends.
    assert PALETTE_VOCABULARY_V1[0] == "background"
    assert "tertiary_fixed_dim" in PALETTE_VOCABULARY_ROLES


def test_probe_fixture_removing_a_role_breaks_the_contract(tmp_path: Path) -> None:
    """Removing a role from either side breaks the pinned equality."""
    payload = json.loads(VOCABULARY_FIXTURE.read_text(encoding="utf-8"))
    payload["roles"] = [role for role in payload["roles"] if role != "on_surface"]
    drift = tmp_path / "drifted.json"
    drift.write_text(json.dumps(payload), encoding="utf-8")
    assert tuple(json.loads(drift.read_text(encoding="utf-8"))["roles"]) != (PALETTE_VOCABULARY_V1)


# ---------------------------------------------------------------------------
# Conformance — colors.yaml + colors.gtk.css
# ---------------------------------------------------------------------------


def test_valid_v1_palette_conforms_and_reports_extras() -> None:
    conformance = check_palette_entry(_fixture("v1-valid"))
    assert conformance.conforming
    assert conformance.semantic
    assert conformance.missing_roles == ()
    assert conformance.malformed_roles == ()
    assert "foreground" in conformance.extra_roles
    assert "accent" in conformance.extra_roles
    conformance.require()  # does not raise


def test_missing_role_fails_naming_the_role() -> None:
    conformance = check_palette_entry(_fixture("v1-missing-role"))
    assert not conformance.conforming
    assert conformance.missing_roles == ("primary",)
    with pytest.raises(PaletteVocabularyError) as excinfo:
        conformance.require()
    assert "primary" in str(excinfo.value)
    assert excinfo.value.missing_roles == ("primary",)


def test_missing_role_in_gtk_css_is_detected() -> None:
    colors_yaml = (_fixture("v1-valid") / "colors.yaml").read_text(encoding="utf-8")
    gtk_css = (_fixture("v1-valid") / "colors.gtk.css").read_text(encoding="utf-8")
    gtk_css = "\n".join(
        line for line in gtk_css.splitlines() if not line.startswith("@define-color primary ")
    )
    conformance = check_palette_artifacts(colors_yaml=colors_yaml, colors_gtk_css=gtk_css)
    assert not conformance.conforming
    assert conformance.missing_roles == ("primary",)
    gtk = next(check for check in conformance.artifacts if check.artifact == "colors.gtk.css")
    assert gtk.missing_roles == ("primary",)


def test_malformed_role_value_fails_naming_the_role(tmp_path: Path) -> None:
    fixture = _fixture("v1-valid")
    colors_yaml = (fixture / "colors.yaml").read_text(encoding="utf-8")
    colors_yaml = colors_yaml.replace('primary: "#26201a"', 'primary: "not-a-color"')
    conformance = check_palette_artifacts(
        colors_yaml=colors_yaml,
        colors_gtk_css=(fixture / "colors.gtk.css").read_text(encoding="utf-8"),
    )
    assert conformance.malformed_roles == ("primary",)
    with pytest.raises(PaletteVocabularyError) as excinfo:
        conformance.require()
    assert "malformed" in str(excinfo.value)
    assert "primary" in str(excinfo.value)


def test_extra_roles_are_allowed_and_reported_info_only() -> None:
    conformance = check_palette_entry(_fixture("v1-extra-roles"))
    assert conformance.conforming
    assert "experimental_surface" in conformance.extra_roles


def test_v040_palette_fails_naming_on_backdrop() -> None:
    """Expected pre-pin behavior: tpr v0.4.0 has no on_backdrop yet."""
    conformance = check_palette_entry(_fixture("v0.4.0"))
    assert not conformance.conforming
    assert conformance.missing_roles == ("on_backdrop",)
    with pytest.raises(PaletteVocabularyError) as excinfo:
        require_palette_conformance(_fixture("v0.4.0"))
    assert "on_backdrop" in str(excinfo.value)


def test_legacy_csg_palette_is_not_a_v1_producer(tmp_path: Path) -> None:
    entry = tmp_path / "csg"
    entry.mkdir()
    (entry / "colors.yaml").write_text(
        'background: "#000000"\nforeground: "#ffffff"\ncursor: "#ffffff"\ncolors:\n  - "#000000"\n',
        encoding="utf-8",
    )
    (entry / "colors.gtk.css").write_text(
        "@define-color color_background #000000;\n@define-color color_00 #000000;\n",
        encoding="utf-8",
    )
    conformance = check_palette_entry(entry)
    assert not conformance.semantic
    assert not conformance.conforming
    assert "surface" in conformance.missing_roles


def test_v1_valid_has_all_51_roles_in_both_artifacts() -> None:
    conformance = check_palette_entry(_fixture("v1-valid"))
    for check in conformance.artifacts:
        assert check.missing_roles == ()
        assert check.present_roles == PALETTE_VOCABULARY_V1


# ---------------------------------------------------------------------------
# Derivation wiring — fail closed BEFORE the cache entry / visible swap
# ---------------------------------------------------------------------------


def test_tpr_build_fails_closed_before_the_cache_entry(tmp_path: Path) -> None:
    stub = _TprPaletteStub(_fixture("v0.4.0"))
    pipeline = _pipeline(tmp_path, stub)
    wallpaper_hash = "ab" * 32
    target = tmp_path / "cache" / "palettes" / stub.expected_entry_hash(wallpaper_hash)
    with pytest.raises(PaletteVocabularyError) as excinfo:
        pipeline.ensure_palette(tmp_path / "wall.png", wallpaper_hash)
    assert excinfo.value.missing_roles == ("on_backdrop",)
    assert "on_backdrop" in str(excinfo.value)
    # Fail closed BEFORE drain/staging rename: no visible entry, no staging.
    assert not target.exists()
    staging = list((tmp_path / "cache").glob(".staging-*"))
    assert staging == []


def test_tpr_cache_hit_is_validated_too(tmp_path: Path) -> None:
    stub = _TprPaletteStub(_fixture("v1-valid"))
    pipeline = _pipeline(tmp_path, stub)
    wallpaper_hash = "ab" * 32
    entry, cache_hit = pipeline.ensure_palette(tmp_path / "wall.png", wallpaper_hash)
    assert not cache_hit

    entry_dir = tmp_path / "cache" / "palettes" / entry.entry_hash
    colors_path = entry_dir / "colors.yaml"
    colors_path.write_text(
        "\n".join(
            line
            for line in colors_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith("on_backdrop:")
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PaletteVocabularyError) as excinfo:
        pipeline.ensure_palette(tmp_path / "wall.png", wallpaper_hash)
    assert excinfo.value.missing_roles == ("on_backdrop",)


def test_csg_producer_is_exempt_from_the_v1_vocabulary(tmp_path: Path) -> None:
    """Legacy csg emits no Material vocabulary; it is not a v1 producer."""
    stub = _TprPaletteStub(_fixture("v0.4.0"))
    stub.producer_name = "csg"
    pipeline = _pipeline(tmp_path, stub)
    entry, cache_hit = pipeline.ensure_palette(tmp_path / "wall.png", "ab" * 32)
    assert not cache_hit
    assert entry.entry_hash


# ---------------------------------------------------------------------------
# Doctor leg — vocabulary conformance is a reportable verdict
# ---------------------------------------------------------------------------


def _doctor_state(entry_hash: str) -> SimpleNamespace:
    return SimpleNamespace(palette=SimpleNamespace(entry_hash=entry_hash))


def test_doctor_reports_nonconforming_palette_as_diverged(tmp_path: Path) -> None:
    entry_hash = "a" * 64
    shutil.copytree(_fixture("v0.4.0"), tmp_path / "cache" / "palettes" / entry_hash)
    doctor = DoctorUseCase(state_repo=None, state_root=tmp_path)  # type: ignore[arg-type]
    items = doctor._check_vocabulary(_doctor_state(entry_hash))
    assert len(items) == 1
    assert items[0].kind == "vocabulary"
    assert items[0].status == "diverged"
    assert "on_backdrop" in items[0].detail
    assert items[0].layer == "palettes"
    assert items[0].entry_hash == entry_hash


def test_doctor_reports_conforming_palette_with_extras(tmp_path: Path) -> None:
    entry_hash = "b" * 64
    shutil.copytree(_fixture("v1-valid"), tmp_path / "cache" / "palettes" / entry_hash)
    doctor = DoctorUseCase(state_repo=None, state_root=tmp_path)  # type: ignore[arg-type]
    items = doctor._check_vocabulary(_doctor_state(entry_hash))
    assert items[0].status == "ok"
    assert "conforming" in items[0].detail
    assert "foreground" in items[0].detail
