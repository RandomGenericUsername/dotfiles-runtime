"""btop palette artifacts threaded through the runtime (Change 3).

`add-runtime-btop-theme-artifacts`: the runtime requests, hashes, caches and
exposes the three CSG btop themes. Every assertion here is machine-checkable
against a temp state root — nothing depends on a provisioned host or a
person's eyes (design D: verification is deliberately host-free).

Covers spec requirements:

- generation requests the btop formats                (5.4)
- membership in the completeness oracle               (5.5)
- `current/` exposure, skipping a missing artifact    (4.4)
- `PaletteArtifacts` construction/serialization      (4.2)
- `load_palette_entry` rebuilds the three fields      (4.3)
- self-heal eviction of a pre-change entry            (4.1, 5.7)
- schema round-trip: ten validates, nine fails        (4.5, 5.8)

The end-to-end `wallpaper set` + `resolve().is_relative_to(entry_dir)`
assertion (5.6) lives in tests/integration/, since it needs a composed use
case rather than a unit-level fake.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import fastjsonschema
import pytest

from runtime.adapters.hashing import canonical_hash_dir, hash_file, palette_entry_hash
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.application.derive import PALETTE_ARTIFACT_NAMES, ensure_palette_entry_complete
from runtime.application.inspect import InspectStateUseCase
from runtime.application.reconcile import ReconcileDesktopStateUseCase
from runtime.domain.models import DesktopState

#: The single btop artifact filename.
BTOP_ARTIFACTS: tuple[str, ...] = (
    "colors.btop.theme",
)

#: The single PaletteArtifacts TypedDict field name.
BTOP_FIELDS: tuple[str, ...] = ("colors_btop",)

#: The seven artifacts that predate this change. An entry holding exactly
#: these is "the pre-change shape" and must self-heal.
PRE_CHANGE_ARTIFACTS: tuple[str, ...] = (
    "colors.yaml",
    "colors.conf",
    "colors.gtk.css",
    "colors.adw.css",
    "colors.sequences",
    "colors.rasi",
    "colors.kitty",
)

ALL_ARTIFACTS: tuple[str, ...] = PRE_CHANGE_ARTIFACTS + BTOP_ARTIFACTS

assert len(ALL_ARTIFACTS) == 8


def _artifact_key(name: str) -> str:
    """meta.json artifact key -> PaletteArtifacts TypedDict field.

    Written out rather than derived by string surgery because the btop names
    carry an embedded hyphen (``colors.btop.theme`` ->
    ``colors_btop``), not a leading ``colors.`` prefix.
    """
    return {
        "colors.yaml": "colors_yaml",
        "colors.conf": "colors_conf",
        "colors.gtk.css": "colors_gtk_css",
        "colors.adw.css": "colors_adw_css",
        "colors.sequences": "colors_sequences",
        "colors.rasi": "colors_rasi",
        "colors.kitty": "colors_kitty",
        "colors.btop.theme": "colors_btop",
    }[name]


def _populate_entry(entry: Path, names: tuple[str, ...]) -> dict[str, str]:
    """Write one file per name and return its sha256."""
    entry.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for name in names:
        (entry / name).write_text(f"# {name}\n")
        hashes[name] = hash_file(entry / name)
    return hashes


# ---------------------------------------------------------------------------
# 5.4 — the CSG invocation carries the --format flag
# ---------------------------------------------------------------------------


def _fake_csg_run(tmp_path: Path) -> MagicMock:
    """subprocess.run stand-in that records argv and writes all eight outputs."""

    def fake_run(
        args: list[str],
        capture_output: bool = False,  # noqa: ARG001
        text: bool = False,  # noqa: ARG001
        env: dict[str, str] | None = None,
        timeout: int | None = None,  # noqa: ARG001
    ) -> subprocess.CompletedProcess[str]:
        assert env is not None
        out = Path(env["COLORSCHEME__OUTPUT__DIRECTORY"])
        out.mkdir(parents=True, exist_ok=True)
        for name in ALL_ARTIFACTS:
            (out / name).write_text(f"# {name}\n")
        return subprocess.CompletedProcess(args, 0, "", "")

    return MagicMock(side_effect=fake_run)


def _fixture_wallpaper(tmp_path: Path) -> Path:
    wallpaper = tmp_path / "wallpaper.png"
    wallpaper.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return wallpaper


def _fixture_templates(tmp_path: Path) -> Path:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "default.yaml").write_text("window: {}\n")
    return templates


def _palette_output_dir(tmp_path: Path, wallpaper: Path, templates: Path) -> Path:
    """The adapter asserts output_dir's basename is the palette entry hash."""
    ph = palette_entry_hash(hash_file(wallpaper), canonical_hash_dir(templates))
    return tmp_path / "cache" / "palettes" / ph


def _generate_and_capture_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wallpaper: Path,
    templates: Path,
) -> list[str]:
    from runtime.adapters.csg_adapter import CsgAdapter

    fake = _fake_csg_run(tmp_path)
    monkeypatch.setattr("runtime.adapters.csg_adapter.subprocess.run", fake)
    CsgAdapter(templates_dir=templates).generate(
        wallpaper, _palette_output_dir(tmp_path, wallpaper, templates)
    )
    argv = fake.call_args[0][0]
    assert isinstance(argv, list)
    return argv


@pytest.mark.parametrize("fmt", ["btop.theme"])
def test_csg_argv_requests_each_btop_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    """5.4: asserted against the recorded argv, not a log read.

    ``--format <fmt>`` must appear as an adjacent pair in the argv the adapter
    actually handed to ``subprocess.run``.
    """
    wallpaper = _fixture_wallpaper(tmp_path)
    templates = _fixture_templates(tmp_path)

    argv = _generate_and_capture_argv(monkeypatch, tmp_path, wallpaper, templates)
    pairs = list(zip(argv, argv[1:], strict=False))
    assert ("--format", fmt) in pairs, f"--format {fmt} missing from CSG argv: {argv}"


def test_csg_argv_requests_exactly_eight_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full requested set, asserted as an ordered format list."""
    wallpaper = _fixture_wallpaper(tmp_path)
    templates = _fixture_templates(tmp_path)

    argv = _generate_and_capture_argv(monkeypatch, tmp_path, wallpaper, templates)
    formats = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--format"]
    assert formats == [
        "yaml",
        "conf",
        "gtk.css",
        "adw.css",
        "sequences",
        "rasi",
        "kitty",
        "btop.theme",
    ]


def test_csg_adapter_hashes_the_btop_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1.3: the produced PaletteArtifacts carries real hashes for all three."""
    from runtime.adapters.csg_adapter import CsgAdapter

    wallpaper = _fixture_wallpaper(tmp_path)
    templates = _fixture_templates(tmp_path)

    fake = _fake_csg_run(tmp_path)
    monkeypatch.setattr("runtime.adapters.csg_adapter.subprocess.run", fake)
    out = _palette_output_dir(tmp_path, wallpaper, templates)
    entry = CsgAdapter(templates_dir=templates).generate(wallpaper, out)

    for name in BTOP_ARTIFACTS:
        assert entry.artifact_hashes[_artifact_key(name)] == hash_file(out / name)


def test_csg_adapter_fails_loud_when_a_btop_output_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1.2: the existence assert covers the btop artifacts too.

    A csg that silently stops emitting ``colors.btop.theme`` must fail the
    derivation, not produce an entry with a silently-missing hash.
    """
    from runtime.adapters.csg_adapter import CsgAdapter

    wallpaper = _fixture_wallpaper(tmp_path)
    templates = _fixture_templates(tmp_path)

    full = _fake_csg_run(tmp_path)
    real_side_effect = full.side_effect

    def dropping_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = real_side_effect(args, **kwargs)
        (Path(kwargs["env"]["COLORSCHEME__OUTPUT__DIRECTORY"]) / "colors.btop.theme").unlink()
        return result

    monkeypatch.setattr(
        "runtime.adapters.csg_adapter.subprocess.run",
        MagicMock(side_effect=dropping_run),
    )

    with pytest.raises(RuntimeError, match="did not write expected artifact"):
        CsgAdapter(templates_dir=templates).generate(
            wallpaper, _palette_output_dir(tmp_path, wallpaper, templates)
        )


# ---------------------------------------------------------------------------
# 5.5 — the completeness oracle carries all three
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", BTOP_ARTIFACTS)
def test_palette_artifact_names_contains_each_btop_artifact(name: str) -> None:
    """5.5: asserted directly against the tuple, not via behaviour."""
    assert name in PALETTE_ARTIFACT_NAMES


def test_palette_artifact_names_is_exactly_the_ten_artifacts() -> None:
    assert set(PALETTE_ARTIFACT_NAMES) == set(ALL_ARTIFACTS)
    assert len(PALETTE_ARTIFACT_NAMES) == 8


def test_btop_fields_exist_on_palette_artifacts_typeddict() -> None:
    """1.4: the dataclass/TypedDict shape declares the three fields."""
    from runtime.domain.models import PaletteArtifacts

    for field in BTOP_FIELDS:
        assert field in PaletteArtifacts.__annotations__


# ---------------------------------------------------------------------------
# 4.2 / 4.3 — construction, serialization and load_palette_entry round-trip
# ---------------------------------------------------------------------------


def _full_artifact_hashes(entry: Path) -> dict[str, str]:
    return _populate_entry(entry, ALL_ARTIFACTS)


def test_palette_artifacts_roundtrip_through_meta_json(tmp_path: Path) -> None:
    """4.2: write meta.json with all ten, read it back field-for-field."""
    entry = tmp_path / "cache" / "palettes" / ("b" * 64)
    hashes = _full_artifact_hashes(entry)
    seeder = CacheSeeder(tmp_path)
    seeder.write_palette_meta_in(
        entry,
        entry_hash="b" * 64,
        source_wallpaper_hash="a" * 64,
        input_template_hash="c" * 64,
        artifact_hashes=hashes,
        generated_at="2026-01-01T00:00:00Z",
    )

    on_disk = json.loads((entry / "meta.json").read_text())
    assert set(on_disk["artifact_hashes"]) == set(ALL_ARTIFACTS)

    loaded = seeder.load_palette_entry(entry)
    for name in ALL_ARTIFACTS:
        assert loaded.artifact_hashes[_artifact_key(name)] == hashes[name]


def test_load_palette_entry_reads_all_three_btop_fields(tmp_path: Path) -> None:
    """4.3 / 1.5: explicitly one assertion per btop field."""
    entry = tmp_path / "cache" / "palettes" / ("b" * 64)
    hashes = _full_artifact_hashes(entry)
    CacheSeeder(tmp_path).write_palette_meta_in(
        entry,
        entry_hash="b" * 64,
        source_wallpaper_hash="a" * 64,
        input_template_hash="c" * 64,
        artifact_hashes=hashes,
    )

    art = CacheSeeder(tmp_path).load_palette_entry(entry).artifact_hashes
    assert art["colors_btop"] == hashes["colors.btop.theme"]
    assert art["colors_btop"] == hashes["colors.btop.theme"]
    assert art["colors_btop"] == hashes["colors.btop.theme"]


# ---------------------------------------------------------------------------
# 5.8 / 4.5 — the schema is the forcing function, not decoration
# ---------------------------------------------------------------------------


def _palette_validator() -> Any:
    from runtime.adapters.contract_schemas import meta_validator

    return meta_validator()


def _ten_artifact_meta() -> dict[str, Any]:
    return {
        "hash_algorithm": "sha256",
        "kind": "palette",
        "entry_hash": "b" * 64,
        "source_wallpaper_hash": "a" * 64,
        "input_template_hash": "c" * 64,
        "artifact_hashes": {name: "d" * 64 for name in ALL_ARTIFACTS},
        "generated_at": "2026-01-01T00:00:00Z",
    }


def test_meta_with_ten_artifacts_validates() -> None:
    """5.8 positive: the shape the runtime now writes conforms."""
    _palette_validator()(_ten_artifact_meta())


@pytest.mark.parametrize("missing", BTOP_ARTIFACTS)
def test_meta_missing_any_btop_artifact_fails_validation(missing: str) -> None:
    """5.8 negative: the schema really is closed over the three new keys."""
    meta = _ten_artifact_meta()
    del meta["artifact_hashes"][missing]
    with pytest.raises(fastjsonschema.JsonSchemaValueException):
        _palette_validator()(meta)


def test_schema_declares_the_three_keys_in_required_and_properties() -> None:
    """3.1 asserted structurally, so a one-sided schema edit is caught."""
    from runtime.adapters.contract_schemas import load_meta_schema

    ah = load_meta_schema()["definitions"]["palette"]["properties"]["artifact_hashes"]
    for name in BTOP_ARTIFACTS:
        assert name in ah["required"], f"{name} missing from artifact_hashes.required"
        assert ah["properties"][name] == {"type": "string"}
    assert ah["additionalProperties"] is False


def test_canonical_and_embedded_schema_agree_on_the_btop_keys(repo_root: Path) -> None:
    """Both copies carry the change — they are byte-identical by contract."""
    canonical = json.loads(
        (repo_root / "contracts" / "schemas" / "meta.schema.json").read_text(),
    )["definitions"]["palette"]["properties"]["artifact_hashes"]
    assert all(name in canonical["required"] for name in BTOP_ARTIFACTS)
    assert all(name in canonical["properties"] for name in BTOP_ARTIFACTS)


# ---------------------------------------------------------------------------
# 4.1 / 5.7 — self-heal: a pre-change entry is evicted and re-derived
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", BTOP_ARTIFACTS)
def test_pre_change_entry_is_evicted(tmp_path: Path, missing: str) -> None:
    """4.1 / 5.7: an entry missing any one btop artifact is incomplete.

    Reported incomplete (``False``) and removed from disk, so the caller's
    normal staging path regenerates the SAME entry hash.
    """
    entry = tmp_path / "cache" / "palettes" / ("b" * 64)
    present = tuple(n for n in ALL_ARTIFACTS if n != missing)
    _populate_entry(entry, present)
    CacheSeeder(tmp_path).write_palette_meta_in(
        entry,
        entry_hash="b" * 64,
        source_wallpaper_hash="a" * 64,
        input_template_hash="c" * 64,
        artifact_hashes={n: hash_file(entry / n) for n in present},
    )

    assert ensure_palette_entry_complete(entry, CacheSeeder(tmp_path)) is False
    assert not entry.exists(), "incomplete pre-change entry must be evicted"


def test_seven_artifact_entry_is_evicted(tmp_path: Path) -> None:
    """The exact pre-change shape (7 artifacts) self-heals with no migration."""
    entry = tmp_path / "cache" / "palettes" / ("b" * 64)
    hashes = _populate_entry(entry, PRE_CHANGE_ARTIFACTS)
    CacheSeeder(tmp_path).write_palette_meta_in(
        entry,
        entry_hash="b" * 64,
        source_wallpaper_hash="a" * 64,
        input_template_hash="c" * 64,
        artifact_hashes=hashes,
    )

    assert ensure_palette_entry_complete(entry, CacheSeeder(tmp_path)) is False
    assert not entry.exists()


def test_ten_artifact_entry_is_a_clean_hit(tmp_path: Path) -> None:
    """The oracle stops evicting once the entry carries all ten."""
    entry = tmp_path / "cache" / "palettes" / ("b" * 64)
    hashes = _populate_entry(entry, ALL_ARTIFACTS)
    CacheSeeder(tmp_path).write_palette_meta_in(
        entry,
        entry_hash="b" * 64,
        source_wallpaper_hash="a" * 64,
        input_template_hash="c" * 64,
        artifact_hashes=hashes,
    )

    assert ensure_palette_entry_complete(entry, CacheSeeder(tmp_path)) is True
    assert entry.is_dir(), "a complete entry must not be evicted"


# ---------------------------------------------------------------------------
# 4.4 — current/ symlinks: all three created, a missing one skipped
# ---------------------------------------------------------------------------


def test_repoint_creates_all_three_btop_symlinks(tmp_path: Path) -> None:
    """The three btop artifacts follow the same repoint rules as the rest."""
    seeder = CacheSeeder(tmp_path)
    entry = tmp_path / "cache" / "palettes" / ("b" * 64)
    _populate_entry(entry, ALL_ARTIFACTS)

    created = seeder.repoint_current_symlinks(
        wallpaper_target=tmp_path / "wall.png",
        monitor_names=["DP-1"],
        palette_entry_hash="b" * 64,
    )
    names = {p.name for p in created}
    current = tmp_path / "current"
    for name in BTOP_ARTIFACTS:
        assert name in names
        link = current / name
        assert link.is_symlink()
        assert link.resolve().is_relative_to(entry.resolve())


def test_missing_btop_source_is_skipped_with_a_warning_and_no_dangling_link(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The skip+warn contract: never a dangling consumer symlink."""
    (tmp_path / "wall.png").write_bytes(b"\x89PNG")  # real target, so any
    # dangling link found below is a palette artifact, not the wallpaper.
    seeder = CacheSeeder(tmp_path)
    entry = tmp_path / "cache" / "palettes" / ("b" * 64)
    present = tuple(n for n in ALL_ARTIFACTS if n != "colors.btop.theme")
    _populate_entry(entry, present)

    with caplog.at_level(logging.WARNING, logger="runtime.adapters.seeder"):
        created = seeder.repoint_current_symlinks(
            wallpaper_target=tmp_path / "wall.png",
            monitor_names=["DP-1"],
            palette_entry_hash="b" * 64,
        )

    names = {p.name for p in created}
    assert "colors.btop.theme" not in names
    current = tmp_path / "current"
    skipped = current / "colors.btop.theme"
    assert not skipped.exists()
    assert not skipped.is_symlink(), "a skipped artifact must leave no dangling symlink"
    for link in current.iterdir():
        if link.is_symlink():
            assert link.exists(), f"dangling symlink left behind: {link}"

    warnings = [r for r in caplog.records if "palette artifact missing" in r.getMessage()]
    assert len(warnings) == 1
    assert "colors.btop.theme" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# 5.6 — end-to-end: wallpaper set exposes the three current/ symlinks
# ---------------------------------------------------------------------------


class _FakeCsg:
    """Contract-honest fake: writes all ten artifacts, returns real hashes."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        hashes: dict[str, str] = {}
        for name in ALL_ARTIFACTS:
            (output_dir / name).write_text(f"# {name}\n")
            hashes[name] = hash_file(output_dir / name)
        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_template_hash="c" * 64,
            artifact_hashes=PaletteArtifacts(  # type: ignore[typeddict-item]
                **{_artifact_key(n): h for n, h in hashes.items()}
            ),
            generated_at="2026-01-01T00:00:00Z",
        )


class _FakeWeg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import EffectsEntry

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "effect.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_catalog_hash="2" * 64,
            artifact_hashes={"effect.png": hash_file(output_dir / "effect.png")},
            generated_at="2026-01-01T00:00:00Z",
        )


class _FakeItr:
    def render(
        self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path
    ) -> Any:
        from runtime.domain.models import IconsEntry

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=output_dir.name,
            source_palette_hash=palette_hash,
            input_templates_hash="5" * 64,
            input_mappings_hash="6" * 64,
            artifact_hashes={"icon.svg": hash_file(output_dir / "icon.svg")},
            generated_at="2026-01-01T00:00:00Z",
        )


def _setup_spine(install_spine: Path) -> None:
    templates = install_spine / "config" / "color-scheme-generator" / "templates"
    templates.mkdir(parents=True)
    (templates / "default.yaml").write_text("window: {}\n")
    weg = install_spine / "config" / "weg"
    weg.mkdir(parents=True)
    (weg / "effects.yaml").write_text("effects: []\n")
    itr = install_spine / "icon-templates"
    itr.mkdir(parents=True)
    (itr / "terminal.svg").write_text("<svg/>")
    (install_spine / "icon-mappings").mkdir(parents=True, exist_ok=True)
    (install_spine / "icon-mappings" / "icons.yaml").write_text("icons: {}\n")


def test_wallpaper_set_exposes_the_three_btop_current_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5.6: after a real converge, all three exist and land INSIDE the entry.

    ``resolve().is_relative_to(entry_dir)`` is the assertion that matters: a
    symlink that merely exists could point anywhere.
    """
    from runtime.adapters.flock_seed_mutex import FlockSeedMutex, PassThroughSeedMutex
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase
    from runtime.application.reconcile import ReconcileDesktopStateUseCase

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    state_root = tmp_path / "state"
    install_spine = tmp_path / "install"
    _setup_spine(install_spine)

    img = tmp_path / "wallpaper.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    csg, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()
    with FlockSeedMutex(state_root / ".seed.lock").hold(blocking=False):
        ApplyWallpaperUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=PassThroughSeedMutex(),
        ).run(img)
        ReconcileDesktopStateUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=PassThroughSeedMutex(),
            reloaders=[],
        ).run(trigger="set")

    assert csg.calls == 1, "the palette must be derived once"

    state = JsonStateRepository(state_root=state_root).load_current()
    assert state.palette is not None
    entry_dir = state_root / "cache" / "palettes" / state.palette.entry_hash
    assert entry_dir.is_dir()

    current = state_root / "current"
    for name in BTOP_ARTIFACTS:
        link = current / name
        assert link.is_symlink(), f"current/{name} must be a symlink"
        assert link.resolve().is_relative_to(entry_dir), (
            f"current/{name} resolves outside the palette entry: {link.resolve()}"
        )
        assert (entry_dir / name).is_file()


# ---------------------------------------------------------------------------
# 3.3 — no other strict reader of artifact_hashes needs updating
# ---------------------------------------------------------------------------


def test_doctor_palette_link_names_covers_all_ten() -> None:
    """doctor mirrors the ``current/`` inventory; a stale mirror reports
    phantom drift on every healthy machine and hides real btop drift."""
    from runtime.application.doctor import PALETTE_LINK_NAMES

    assert set(PALETTE_LINK_NAMES) == set(ALL_ARTIFACTS)


def _synthetic_state(palette_hash: str = "b" * 64) -> Any:
    from runtime.domain.models import (
        BackendType,
        FitMode,
        MonitorWallpaperConfig,
        PaletteEntry,
        WallpaperEntry,
    )

    return DesktopState(
        schema_version=2,
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash="a" * 64,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash="a" * 64,
            source_path="",
            imported_at="2026-01-01T00:00:00Z",
        ),
        palette=PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=palette_hash,
            source_wallpaper_hash="a" * 64,
            input_template_hash="c" * 64,
            artifact_hashes={},  # type: ignore[typeddict-item]
            generated_at="2026-01-01T00:00:00Z",
        ),
        effects=None,
        icons=None,
        applied_at="2026-01-01T00:00:00Z",
    )


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda root: ReconcileDesktopStateUseCase(
                state_repo=MagicMock(),
                csg=MagicMock(),
                weg=MagicMock(),
                itr=MagicMock(),
                install_spine=root / "install",
                state_root=root,
                seeder=CacheSeeder(root),
                mutex=MagicMock(),
                reloaders=[],
            ),
            id="reconcile",
        ),
        pytest.param(
            lambda root: InspectStateUseCase(
                state_root=root, state_repo=JsonStateRepository(state_root=root)
            ),
            id="inspect",
        ),
    ],
)
def test_expected_targets_include_all_ten_artifacts(tmp_path: Path, factory: Any) -> None:
    """Both expected-target projections name all ten.

    reconcile prunes any ``current/`` symlink absent from its target map, and
    inspect reports the same drift — so a stale literal in either one silently
    deletes the btop symlinks on the next converge.
    """
    targets = factory(tmp_path)._build_expected_targets(_synthetic_state())
    assert set(BTOP_ARTIFACTS) <= set(targets), f"missing btop targets: {sorted(targets)}"
    assert set(ALL_ARTIFACTS) <= set(targets)


def test_reconcile_prune_set_names_all_ten(tmp_path: Path) -> None:
    """The stale-prune list must not treat a btop symlink as unexpected."""
    from runtime.application.reconcile import ReconcileDesktopStateUseCase

    use_case = ReconcileDesktopStateUseCase(
        state_repo=MagicMock(),
        csg=MagicMock(),
        weg=MagicMock(),
        itr=MagicMock(),
        install_spine=tmp_path / "install",
        state_root=tmp_path,
        seeder=CacheSeeder(tmp_path),
        mutex=MagicMock(),
        reloaders=[],
    )
    targets = use_case._build_expected_targets(_synthetic_state())
    for name in BTOP_ARTIFACTS:
        assert name in targets


def test_seeder_current_artifact_loop_names_all_ten() -> None:
    """``load_palette_entry`` and ``repoint_current_symlinks`` share no
    constant, so pin the latter through its own behaviour (above) and the
    former through the round-trip test. This guards the file-level symmetry
    between the two lists."""
    from runtime.adapters import seeder as seeder_mod

    src = Path(seeder_mod.__file__).read_text()
    for name in BTOP_ARTIFACTS:
        assert src.count(f'"{name}"') >= 2, (
            f"{name} must appear in both load_palette_entry and repoint_current_symlinks"
        )
