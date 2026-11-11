"""Phase 1 host defaults: adapter, resolver, identity, GC and doctor."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from runtime.adapters.appearance import AppearanceGenerator
from runtime.adapters.cache import cache_entry_path
from runtime.adapters.contract_schemas import (
    appearance_json_validator,
    generation_manifest_validator,
)
from runtime.adapters.generation_store import (
    GenerationManifestError,
    GenerationSpec,
    GenerationStore,
    generation_intent_hash,
)
from runtime.adapters.hashing import (
    appearance_generation_hash,
    canonical_hash_dir,
    hash_file,
    skin_entry_hash,
)
from runtime.adapters.hyprland_host_defaults import HyprlandHostDefaults
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.adapters.skin_expander import (
    SkinExpander,
    expand_v2_template,
    load_skin_manifest,
    resolve_v2_token_document_with_host,
    validate_v2_manifest,
)
from runtime.application.doctor import DoctorUseCase
from runtime.domain.host_defaults import (
    HOST_MAP,
    HOST_MAP_VERSION,
    HostDefaultsError,
    HostSnapshot,
    resolve_host_defaults,
    validate_host_value,
)
from runtime.domain.models import (
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
    WallpaperEntry,
)
from tests.unit.test_appearance_v2_assets import (
    _TEMPLATES,
    _WH,
    _spine,
    _write_palette,
    _write_skin,
)

_MANIFESTS = Path(__file__).resolve().parents[1] / "fixtures" / "skins-v2" / "manifests"
_HOST_PATHS = ("surface.panel.radius", "surface.panel.border-width")
_RECT_PATHS = (
    "surface.card.radius",
    "surface.control.radius",
    "surface.cell.radius",
)
_ALL_HOST_PATHS = (*_HOST_PATHS, *_RECT_PATHS)
_TS = "2026-11-09T00:00:00Z"


class _Runner:
    """Small subprocess.run stand-in keyed by argv after the binary."""

    def __init__(self, responses: dict[tuple[str, ...], subprocess.CompletedProcess[str]]):
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args, **_kwargs):
        key = tuple(args[1:])
        self.calls.append(key)
        return self.responses.get(
            key,
            subprocess.CompletedProcess(args, 1, "", "missing"),
        )


def _ok(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, json.dumps(payload), "")


def _snapshot(
    radius: int = 8,
    border: int = 2,
    *,
    card: int | None = None,
    control: int | None = None,
    cell: int | None = None,
) -> HostSnapshot:
    return HostSnapshot(
        values={
            "surface.panel.radius": radius,
            "surface.panel.border-width": border,
            "surface.card.radius": radius if card is None else card,
            "surface.control.radius": radius if control is None else control,
            "surface.cell.radius": radius if cell is None else cell,
        }
    )


def _manifest(name: str) -> dict[str, object]:
    return load_skin_manifest(_MANIFESTS / f"{name}.toml")


def _manifest_with_inherit(name: str, paths: tuple[str, ...]) -> dict[str, object]:
    manifest = json.loads(json.dumps(_manifest(name)))
    manifest["host"]["inherit"] = list(paths)
    return manifest


def _write_manifest(tmp_path: Path, name: str, paths: tuple[str, ...]) -> Path:
    source = _MANIFESTS / f"{name}.toml"
    text = source.read_text(encoding="utf-8")
    old = 'inherit = ["surface.panel.radius", "surface.panel.border-width"]'
    new = "inherit = [" + ", ".join(f'"{path}"' for path in paths) + "]"
    assert old in text
    manifest_path = tmp_path / f"{name}-host-rectangles.toml"
    manifest_path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return manifest_path


def _write_skin_with_manifest(
    state_root: Path, name: str, manifest_path: Path
) -> tuple[object, Path]:
    manifest = load_skin_manifest(manifest_path)
    validate_v2_manifest(manifest, name, manifest_path)
    expected = skin_entry_hash(canonical_hash_dir(_TEMPLATES), hash_file(manifest_path))
    work = cache_entry_path(state_root, "skins", expected)
    entry = SkinExpander().expand(name, manifest_path, _TEMPLATES, work)
    CacheSeeder(state_root).write_skin_meta(
        entry_hash=entry.entry_hash,
        name=entry.name,
        input_templates_hash=entry.input_templates_hash,
        input_manifest_hash=entry.input_manifest_hash,
        artifact_hashes=dict(entry.artifact_hashes),
        generated_at=entry.generated_at,
    )
    return entry, manifest_path


def _token(tokens: dict[str, object], path: str) -> object:
    cursor: object = tokens
    for part in path.split("."):
        assert isinstance(cursor, dict)
        cursor = cursor[part]
    return cursor


# ---------------------------------------------------------------- resolver


def test_host_fingerprint_is_only_participating_values() -> None:
    one = HostSnapshot(values={"surface.panel.radius": 8, "surface.panel.border-width": 2})
    two = HostSnapshot(
        values={
            "surface.panel.border-width": 2,
            "surface.panel.radius": 8,
            "ignored": 99,
        }
    )
    assert one.fingerprint(_HOST_PATHS) == two.fingerprint(_HOST_PATHS)
    assert one.fingerprint(("surface.panel.radius",)) == HostSnapshot(
        values={"surface.panel.radius": 8}
    ).fingerprint(("surface.panel.radius",))
    assert one.fingerprint(_HOST_PATHS) != two.fingerprint(("surface.panel.radius",))
    assert HostSnapshot().fingerprint() is None
    # Instance signatures/timestamps do not participate.
    assert HostSnapshot(values={"surface.panel.radius": 8}, instance_signature="a").fingerprint(
        ("surface.panel.radius",)
    ) == HostSnapshot(values={"surface.panel.radius": 8}, instance_signature="b").fingerprint(
        ("surface.panel.radius",)
    )


@pytest.mark.parametrize("name", ["glass", "paper", "terminal", "flat"])
def test_resolver_precedence_and_no_host_policy(name: str) -> None:
    manifest = _manifest(name)
    snapshot = _snapshot(8, 2)
    for mode in ("dark", "light"):
        tokens, resolution = resolve_v2_token_document_with_host(manifest, mode, snapshot)
        panel = tokens["surface"]["panel"]
        if name in ("glass", "paper") and not (name == "paper" and mode == "light"):
            assert panel["radius"] == 8
            assert panel["border-width"] == 2
            assert resolution.provenance == {
                "surface.panel.radius": "host",
                "surface.panel.border-width": "host",
            }
            assert resolution.host_hash == snapshot.fingerprint(_HOST_PATHS)
        elif name == "paper":
            # The complete authored mode.light surface group is explicit.
            assert panel["radius"] == 6
            assert panel["border-width"] == 1
            assert resolution.provenance == {
                "surface.panel.radius": "mode-override",
                "surface.panel.border-width": "mode-override",
            }
            assert resolution.host_hash is None
        else:
            assert "host" not in manifest
            assert resolution.provenance == {}
            assert resolution.host_hash is None


def test_resolver_partial_host_values_fall_back_per_token() -> None:
    manifest = _manifest("glass")
    tokens, resolution = resolve_v2_token_document_with_host(
        manifest,
        "dark",
        HostSnapshot(values={"surface.panel.radius": 9}),
    )
    assert tokens["surface"]["panel"]["radius"] == 9
    assert tokens["surface"]["panel"]["border-width"] == 1.5
    assert resolution.provenance == {
        "surface.panel.radius": "host",
        "surface.panel.border-width": "skin-fallback",
    }
    assert resolution.host_hash == HostSnapshot(values={"surface.panel.radius": 9}).fingerprint(
        ("surface.panel.radius",)
    )


def test_resolver_applies_all_five_rectangle_paths() -> None:
    manifest = _manifest_with_inherit("glass", _ALL_HOST_PATHS)
    snapshot = HostSnapshot(
        values={
            "surface.panel.radius": 8,
            "surface.panel.border-width": 2,
            "surface.card.radius": 12,
            "surface.control.radius": 10,
            "surface.cell.radius": 6,
        }
    )
    tokens, resolution = resolve_v2_token_document_with_host(manifest, "dark", snapshot)
    assert _token(tokens, "surface.panel.radius") == 8
    assert _token(tokens, "surface.panel.border-width") == 2
    assert _token(tokens, "surface.card.radius") == 12
    assert _token(tokens, "surface.control.radius") == 10
    assert _token(tokens, "surface.cell.radius") == 6
    assert resolution.provenance == {path: "host" for path in _ALL_HOST_PATHS}
    assert resolution.defaults == dict(snapshot.values)
    assert resolution.host_hash == snapshot.fingerprint(_ALL_HOST_PATHS)


@pytest.mark.parametrize("path", _RECT_PATHS)
def test_resolver_applies_each_rectangle_radius_individually(path: str) -> None:
    manifest = _manifest_with_inherit("glass", (path,))
    snapshot = HostSnapshot(values={path: 13})
    tokens, resolution = resolve_v2_token_document_with_host(manifest, "dark", snapshot)
    assert _token(tokens, path) == 13
    assert resolution.provenance == {path: "host"}
    assert resolution.defaults == {path: 13}
    assert resolution.host_hash == snapshot.fingerprint((path,))
    # The other rectangle radii keep their skin values.
    for other in _RECT_PATHS:
        if other != path:
            assert _token(tokens, other) == _token(
                resolve_v2_token_document_with_host(manifest, "dark", None)[0], other
            )


def test_pill_radius_is_not_delegable() -> None:
    assert "surface.pill.radius" not in HOST_MAP
    with pytest.raises(ValueError, match="known-path"):
        resolve_host_defaults(
            _manifest("glass")["groups"],
            _manifest_with_inherit("glass", ("surface.pill.radius",)),
            "dark",
            _snapshot(),
        )
    with pytest.raises(ValueError, match="unknown host token path"):
        validate_host_value("surface.pill.radius", 20)


def test_paper_light_mode_override_wins_for_new_rectangle_paths() -> None:
    manifest = _manifest_with_inherit("paper", _ALL_HOST_PATHS)
    tokens, resolution = resolve_v2_token_document_with_host(manifest, "light", _snapshot(16, 4))
    assert _token(tokens, "surface.card.radius") == 4
    assert _token(tokens, "surface.control.radius") == 3
    assert _token(tokens, "surface.cell.radius") == 3
    assert resolution.provenance == {path: "mode-override" for path in _ALL_HOST_PATHS}
    assert resolution.host_hash is None


def test_new_rectangle_paths_fall_back_per_token_and_headless() -> None:
    manifest = _manifest_with_inherit("glass", _ALL_HOST_PATHS)
    tokens, resolution = resolve_v2_token_document_with_host(
        manifest, "dark", HostSnapshot(values={"surface.card.radius": 12})
    )
    assert _token(tokens, "surface.card.radius") == 12
    assert resolution.provenance == {
        "surface.panel.radius": "skin-fallback",
        "surface.panel.border-width": "skin-fallback",
        "surface.card.radius": "host",
        "surface.control.radius": "skin-fallback",
        "surface.cell.radius": "skin-fallback",
    }
    assert resolution.host_hash == HostSnapshot(
        values={"surface.card.radius": 12}
    ).fingerprint(("surface.card.radius",))

    headless, headless_resolution = resolve_v2_token_document_with_host(
        manifest, "dark", None
    )
    assert headless == resolve_v2_token_document_with_host(manifest, "dark", None)[0]
    assert headless_resolution.provenance == {
        path: "skin-fallback" for path in _ALL_HOST_PATHS
    }
    assert headless_resolution.host_hash is None

    fallback_error = _manifest_with_inherit("glass", _ALL_HOST_PATHS)
    fallback_error["host"]["fallback"] = "error"
    with pytest.raises(HostDefaultsError, match="surface.card.radius"):
        resolve_host_defaults(
            fallback_error["groups"],
            fallback_error,
            "dark",
            HostSnapshot(
                values={
                    "surface.panel.radius": 8,
                    "surface.panel.border-width": 2,
                    "surface.control.radius": 10,
                    "surface.cell.radius": 6,
                }
            ),
        )


def test_new_rectangle_values_do_not_churn_a_panel_only_manifest() -> None:
    manifest = _manifest("glass")
    tokens, resolution = resolve_v2_token_document_with_host(manifest, "dark", _snapshot(8, 2))
    panel_values = {
        "surface.panel.radius": 8,
        "surface.panel.border-width": 2,
    }
    panel_only = HostSnapshot(values=panel_values)
    assert resolution.defaults == panel_values
    assert resolution.host_hash == panel_only.fingerprint(_HOST_PATHS)
    assert HOST_MAP_VERSION == 1
    assert tokens == resolve_v2_token_document_with_host(
        manifest, "dark", panel_only
    )[0]


def test_rofi_like_rectangle_tokens_follow_host_but_pill_stays() -> None:
    manifest = _manifest_with_inherit("glass", _ALL_HOST_PATHS)
    snapshot = _snapshot(8, 2, card=12, control=10, cell=6)
    tokens, _resolution = resolve_v2_token_document_with_host(manifest, "dark", snapshot)
    template = "\n".join(
        (
            "* {",
            "  card: {{SKIN_SURFACE_CARD_RADIUS}};",
            "  control: {{SKIN_SURFACE_CONTROL_RADIUS}};",
            "  cell: {{SKIN_SURFACE_CELL_RADIUS}};",
            "  pill: {{SKIN_SURFACE_PILL_RADIUS}};",
            "}",
        )
    )
    rendered = expand_v2_template(template, tokens, format="rasi")
    assert "card: 12px;" in rendered
    assert "control: 10px;" in rendered
    assert "cell: 6px;" in rendered
    # surface.pill.radius is never inherited: glass keeps its authored 20.
    assert "pill: 20px;" in rendered


def test_resolver_fallback_error_fails_closed() -> None:
    manifest = _manifest("glass")
    manifest = json.loads(json.dumps(manifest))
    manifest["host"]["fallback"] = "error"
    with pytest.raises(HostDefaultsError, match="surface.panel.border-width"):
        resolve_host_defaults(
            manifest["groups"],
            manifest,
            "dark",
            HostSnapshot(values={"surface.panel.radius": 8}),
        )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("surface.panel.radius", True),
        ("surface.panel.radius", "8"),
        ("surface.panel.radius", -1),
        ("surface.panel.radius", 65),
        ("surface.panel.border-width", float("nan")),
        ("surface.panel.border-width", float("inf")),
        ("surface.panel.border-width", []),
        ("surface.panel.border-width", {"css": "5 5 5 5"}),
        ("surface.card.radius", True),
        ("surface.card.radius", "8"),
        ("surface.card.radius", -1),
        ("surface.card.radius", 65),
        ("surface.control.radius", float("nan")),
        ("surface.control.radius", 65),
        ("surface.cell.radius", -1),
        ("surface.cell.radius", 65),
    ],
)
def test_host_value_validation_rejects_non_scalars(path: str, value: object) -> None:
    with pytest.raises(ValueError):
        validate_host_value(path, value)


@pytest.mark.parametrize("path", _RECT_PATHS)
@pytest.mark.parametrize("value", [0, 64, 7, 7.5])
def test_new_rectangle_radius_bounds_accept_zero_to_sixty_four(
    path: str, value: float
) -> None:
    assert validate_host_value(path, value) == value


def test_appearance_v3_formula_uses_the_dash_placeholder() -> None:
    import hashlib

    palette = "a" * 64
    skin = "b" * 64
    host_hash = "c" * 64
    expected_host = hashlib.sha256(
        f"appearance.v3\n{palette}\n{skin}\ndark\n{host_hash}".encode()
    ).hexdigest()
    expected_none = hashlib.sha256(
        f"appearance.v3\n{palette}\n{skin}\ndark\n-".encode()
    ).hexdigest()
    assert appearance_generation_hash(palette, skin, "dark", host_hash=host_hash) == expected_host
    assert appearance_generation_hash(palette, skin, "dark") == expected_none


def test_non_delegated_host_change_does_not_churn() -> None:
    manifest = _manifest("terminal")
    first, r1 = resolve_v2_token_document_with_host(manifest, "dark", _snapshot(0, 1))
    second, r2 = resolve_v2_token_document_with_host(manifest, "dark", _snapshot(64, 16))
    assert first == second
    assert r1.host_hash == r2.host_hash is None

    paper = _manifest("paper")
    light1, p1 = resolve_v2_token_document_with_host(paper, "light", _snapshot(0, 1))
    light2, p2 = resolve_v2_token_document_with_host(paper, "light", _snapshot(16, 4))
    assert light1 == light2
    assert p1.host_hash == p2.host_hash is None


# ---------------------------------------------------------------- adapter


def test_adapter_reads_effective_values_with_signature() -> None:
    runner = _Runner(
        {
            ("-j", "getoption", "decoration:rounding"): _ok({"int": 0}),
            ("-j", "getoption", "general:border_size"): _ok({"int": 1}),
        }
    )
    adapter = HyprlandHostDefaults(
        Path("/bin/true"),
        env={"HYPRLAND_INSTANCE_SIGNATURE": "sig"},
        runner=runner,
    )
    snapshot = adapter.read()
    assert snapshot is not None
    assert snapshot.values == {
        "surface.panel.radius": 0,
        "surface.panel.border-width": 1,
        "surface.card.radius": 0,
        "surface.control.radius": 0,
        "surface.cell.radius": 0,
    }
    assert snapshot.instance_signature == "sig"
    assert not any(call[:2] == ("-j", "instances") for call in runner.calls)
    # The three inner radii share decoration:rounding, so it is read once.
    assert runner.calls.count(("-j", "getoption", "decoration:rounding")) == 1


def test_adapter_discovers_a_single_instance_when_signature_is_absent() -> None:
    runner = _Runner(
        {
            ("-j", "instances"): _ok([{"instance": "discovered"}]),
            ("-j", "getoption", "decoration:rounding"): _ok({"int": 8}),
            ("-j", "getoption", "general:border_size"): _ok({"float": 1.5}),
        }
    )
    adapter = HyprlandHostDefaults(Path("/bin/true"), env={}, runner=runner)
    snapshot = adapter.read()
    assert snapshot is not None
    assert snapshot.values == {
        "surface.panel.radius": 8,
        "surface.panel.border-width": 1.5,
        "surface.card.radius": 8,
        "surface.control.radius": 8,
        "surface.cell.radius": 8,
    }
    assert snapshot.instance_signature == "discovered"


@pytest.mark.parametrize("instances", [[], [{"instance": "a"}, {"instance": "b"}]])
def test_adapter_zero_or_many_instances_are_unavailable(instances: list[dict[str, str]]) -> None:
    runner = _Runner({("-j", "instances"): _ok(instances)})
    adapter = HyprlandHostDefaults(Path("/bin/true"), env={}, runner=runner)
    assert adapter.read() is None


def test_adapter_rejects_invalid_scalars_and_vectors() -> None:
    runner = _Runner(
        {
            ("-j", "getoption", "decoration:rounding"): _ok({"int": True}),
            ("-j", "getoption", "general:border_size"): _ok({"css": "5 5 5 5"}),
        }
    )
    adapter = HyprlandHostDefaults(
        Path("/bin/true"),
        env={"HYPRLAND_INSTANCE_SIGNATURE": "sig"},
        runner=runner,
    )
    snapshot = adapter.read()
    assert snapshot is not None
    assert snapshot.values == {}
    # One warning per delegated path: four radii all read the invalid
    # decoration:rounding, plus the panel border width.
    assert len(snapshot.warnings) == 5


def test_adapter_missing_binary_is_unavailable() -> None:
    adapter = HyprlandHostDefaults(Path("/nonexistent/hyprctl"), env={})
    assert adapter.read() is None


# ------------------------------------------------------- composed identity


def _state(state_root: Path, palette, skin) -> DesktopState:
    wall = cache_entry_path(state_root, "wallpapers", _WH)
    wall.mkdir(parents=True, exist_ok=True)
    (wall / "wallpaper.png").write_bytes(b"wallpaper")
    CacheSeeder(state_root).write_wallpaper_meta(
        wallpaper_hash=_WH, source_path="/img/wall.png"
    )
    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=_WH,
            source_path="/img/wall.png",
            imported_at=_TS,
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=_WH,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=palette,
        effects=None,
        icons=None,
        applied_at=_TS,
        skin=skin,
    )


def _compose(
    tmp_path: Path,
    name: str,
    *,
    host: HostSnapshot | None,
    mode: str = "dark",
) -> tuple[object, Path, Path, object]:
    state_root = tmp_path / "state"
    palette_hash = "a" * 64 if mode == "dark" else "b" * 64
    palette = _write_palette(state_root, palette_hash, mode)
    skin, manifest_path = _write_skin(state_root, name)
    spine = _spine(tmp_path)
    entry, _hit = AppearanceGenerator(state_root).ensure(
        palette_entry=palette,
        skin_entry=skin,
        install_spine=spine,
        manifest_path=manifest_path,
        templates_dir=_TEMPLATES,
        host=host,
    )
    return entry, state_root, spine, skin


def _commit(state_root: Path, entry, skin) -> tuple[str, dict[str, object]]:
    state = _state(state_root, None, skin)  # replace palette below
    # Reuse the real palette entry written by _compose.
    from runtime.adapters.cache import cache_entry_path

    palette_dir = cache_entry_path(state_root, "palettes", "a" * 64)
    assert palette_dir.is_dir()
    from runtime.domain.models import PaletteEntry

    palette = PaletteEntry(
        hash_algorithm="sha256",
        kind="palette",
        entry_hash="a" * 64,
        source_wallpaper_hash=_WH,
        input_template_hash="t" * 64,
        artifact_hashes={},  # type: ignore[typeddict-item]
        generated_at=_TS,
    )
    state = _state(state_root, palette, skin)
    JsonStateRepository(state_root=state_root).save(state)
    result = GenerationStore(state_root).commit(
        GenerationSpec(
            state=state,
            appearance=entry,  # type: ignore[arg-type]
            mode="dark",
            skin_name=skin.name,
            skin_hash=skin.entry_hash,
            intent_hash=generation_intent_hash(
                wallpaper=_WH,
                palette="a" * 64,
                skin=skin.name,
                mode="dark",
                host=entry.host_hash,  # type: ignore[attr-defined]
            ),
            monitor_names=("DP-1",),
            source_kind="cli",
            host_hash=entry.host_hash,  # type: ignore[attr-defined]
            host_values=entry.host_values,  # type: ignore[attr-defined]
        )
    )
    return result.generation_hash, result.manifest


def test_composed_appearance_uses_host_values_and_v3_identity(tmp_path: Path) -> None:
    entry, state_root, _spine_path, _skin = _compose(tmp_path, "glass", host=_snapshot(8, 2))
    assert entry.host_hash == _snapshot(8, 2).fingerprint(_HOST_PATHS)
    cache_dir = state_root / "cache" / "appearance" / entry.appearance_hash
    tokens = json.loads((cache_dir / "tokens.json").read_text(encoding="utf-8"))
    appearance_json_validator()(tokens)
    assert tokens["surface"]["panel"]["radius"] == 8
    assert tokens["surface"]["panel"]["border-width"] == 2
    css = (cache_dir / "ags.css").read_text(encoding="utf-8")
    assert "border-radius: 8px;" in css
    assert "border: 2px solid @primary;" in css
    assert entry.appearance_hash == appearance_generation_hash(
        "a" * 64, entry.skin_hash, "dark", host_hash=entry.host_hash
    )


def test_paper_light_ignores_host_and_uses_no_host_identity(tmp_path: Path) -> None:
    entry, state_root, _spine_path, _skin = _compose(
        tmp_path, "paper", host=_snapshot(8, 2), mode="light"
    )
    assert entry.host_hash is None
    assert entry.host_values == ()
    tokens = json.loads(
        (state_root / "cache" / "appearance" / entry.appearance_hash / "tokens.json").read_text(
            encoding="utf-8"
        )
    )
    assert tokens["surface"]["panel"]["radius"] == 6


def test_generation_identity_includes_host_but_queue_key_is_host_free() -> None:
    from runtime.application.generation_converge import intent_hash_for

    common = {
        "wallpaper": "a" * 64,
        "palette": "b" * 64,
        "skin": "glass",
        "mode": "dark",
    }
    first = generation_intent_hash(**common, host="c" * 64)
    second = generation_intent_hash(**common, host="d" * 64)
    assert first != second
    assert generation_intent_hash(**common) != first
    queue = intent_hash_for(
        wallpaper=common["wallpaper"],
        skin=common["skin"],
        mode=common["mode"],
    )
    assert queue == intent_hash_for(
        wallpaper=common["wallpaper"],
        skin=common["skin"],
        mode=common["mode"],
    )


def test_generation_writes_host_block_and_intent_host(tmp_path: Path) -> None:
    entry, state_root, _spine_path, skin = _compose(tmp_path, "glass", host=_snapshot(8, 2))
    _generation, manifest = _commit(state_root, entry, skin)
    assert manifest["host"] == {
        "source": "hyprland",
        "version": 1,
        "fingerprint": entry.host_hash,
        "defaults": {
            "surface.panel.radius": 8,
            "surface.panel.border-width": 2,
        },
    }
    assert manifest["intent"]["host"] == entry.host_hash
    generation_manifest_validator()(manifest)


def test_generation_writes_all_rectangle_host_defaults(tmp_path: Path) -> None:
    from fastjsonschema import JsonSchemaValueException

    state_root = tmp_path / "state"
    palette = _write_palette(state_root, "a" * 64, "dark")
    manifest_path = _write_manifest(tmp_path, "glass", _ALL_HOST_PATHS)
    skin, _path = _write_skin_with_manifest(state_root, "glass", manifest_path)
    snapshot = HostSnapshot(
        values={
            "surface.panel.radius": 8,
            "surface.panel.border-width": 2,
            "surface.card.radius": 12,
            "surface.control.radius": 10,
            "surface.cell.radius": 6,
        }
    )
    entry, _hit = AppearanceGenerator(state_root).ensure(
        palette_entry=palette,
        skin_entry=skin,
        install_spine=_spine(tmp_path),
        manifest_path=manifest_path,
        templates_dir=_TEMPLATES,
        host=snapshot,
    )
    _generation, manifest = _commit(state_root, entry, skin)
    assert manifest["host"]["version"] == HOST_MAP_VERSION == 1
    assert manifest["host"]["defaults"] == {
        path: snapshot.values[path] for path in _ALL_HOST_PATHS
    }
    assert manifest["intent"]["host"] == entry.host_hash
    assert entry.host_hash == snapshot.fingerprint(_ALL_HOST_PATHS)
    generation_manifest_validator()(manifest)

    pill = json.loads(json.dumps(manifest))
    pill["host"]["defaults"]["surface.pill.radius"] = 20
    with pytest.raises(JsonSchemaValueException):
        generation_manifest_validator()(pill)


def test_generation_rejects_intent_host_mismatch(tmp_path: Path) -> None:
    entry, state_root, _spine_path, skin = _compose(tmp_path, "glass", host=_snapshot(8, 2))
    generation, manifest = _commit(state_root, entry, skin)
    manifest["intent"]["host"] = "b" * 64
    path = state_root / "generations" / generation / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(GenerationManifestError, match="no valid manifest"):
        GenerationStore(state_root).verify(generation)


def test_gc_protects_host_aware_appearance_cache_entry(tmp_path: Path) -> None:
    first, state_root, spine, skin = _compose(tmp_path, "glass", host=_snapshot(8, 2))
    # A newer, non-referenced appearance entry must not displace the committed
    # host-aware entry from GC protection.
    from runtime.domain.models import PaletteEntry

    palette = PaletteEntry(
        hash_algorithm="sha256",
        kind="palette",
        entry_hash="a" * 64,
        source_wallpaper_hash=_WH,
        input_template_hash="t" * 64,
        artifact_hashes={},  # type: ignore[typeddict-item]
        generated_at=_TS,
    )
    second, _hit = AppearanceGenerator(state_root).ensure(
        palette_entry=palette,
        skin_entry=skin,
        install_spine=spine,
        manifest_path=_MANIFESTS / "glass.toml",
        templates_dir=_TEMPLATES,
        host=_snapshot(12, 3),
    )
    assert first.appearance_hash != second.appearance_hash
    generation, _manifest = _commit(state_root, first, skin)
    store = GenerationStore(state_root)
    store.gc(keep=1, grace_seconds=0, current_generation=generation)
    assert (state_root / "cache" / "appearance" / first.appearance_hash).is_dir()


class _FakeHost:
    def __init__(self, snapshot: HostSnapshot | None) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def read(self) -> HostSnapshot | None:
        self.calls += 1
        return self.snapshot


def test_doctor_reports_host_drift_only_for_delegated_values(tmp_path: Path) -> None:
    entry, state_root, spine, skin = _compose(tmp_path, "glass", host=_snapshot(8, 2))
    _commit(state_root, entry, skin)
    (spine / "config" / "wlogout" / "appearance.css").symlink_to(
        state_root / "current" / "appearance" / "wlogout.css"
    )
    probe = _FakeHost(_snapshot(9, 2))
    items = DoctorUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
        install_spine=spine,
        host_probe=probe,
    ).check().items
    by_name = {item.name: item for item in items}
    assert by_name["generation:host"].status == "diverged"
    assert "consumer/config/wlogout/appearance.css" not in by_name

    matching = _FakeHost(_snapshot(8, 2))
    items = DoctorUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
        install_spine=spine,
        host_probe=matching,
    ).check().items
    by_name = {item.name: item for item in items}
    assert by_name["generation:host"].status == "ok"
    assert "consumer/config/wlogout/appearance.css" not in by_name

    unavailable = _FakeHost(None)
    items = DoctorUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
        install_spine=spine,
        host_probe=unavailable,
    ).check().items
    by_name = {item.name: item for item in items}
    assert by_name["generation:host"].status == "ok"
    assert "unknown" in by_name["generation:host"].detail


def test_doctor_host_drift_covers_new_rectangle_paths_and_ignores_pill(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    palette = _write_palette(state_root, "a" * 64, "dark")
    manifest_path = _write_manifest(tmp_path, "glass", _ALL_HOST_PATHS)
    skin, _path = _write_skin_with_manifest(state_root, "glass", manifest_path)
    spine = _spine(tmp_path)
    committed = HostSnapshot(
        values={
            "surface.panel.radius": 8,
            "surface.panel.border-width": 2,
            "surface.card.radius": 12,
            "surface.control.radius": 10,
            "surface.cell.radius": 6,
        }
    )
    entry, _hit = AppearanceGenerator(state_root).ensure(
        palette_entry=palette,
        skin_entry=skin,
        install_spine=spine,
        manifest_path=manifest_path,
        templates_dir=_TEMPLATES,
        host=committed,
    )
    _commit(state_root, entry, skin)

    def _doctor(snapshot: HostSnapshot) -> dict[str, object]:
        items = DoctorUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            state_root=state_root,
            install_spine=spine,
            host_probe=_FakeHost(snapshot),
        ).check().items
        return {item.name: item for item in items}

    drifted = HostSnapshot(values={**dict(committed.values), "surface.cell.radius": 7})
    assert _doctor(drifted)["generation:host"].status == "diverged"

    # An extra pill or any other non-delegated value is not compared.
    non_delegated = HostSnapshot(
        values={
            **dict(committed.values),
            "surface.pill.radius": 999,
            "surface.card.border-width": 3,
            "decoration:blur:size": 9,
        }
    )
    assert _doctor(non_delegated)["generation:host"].status == "ok"
