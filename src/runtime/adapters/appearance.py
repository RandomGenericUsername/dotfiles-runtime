"""Compose the one v2 appearance generation (contracts v0.7.0).

The runtime keeps two independent content-addressed caches — palettes and
skins — and publishes their composition as one immutable generation::

    cache/appearance/<appearanceHash>/
        colors.yaml          # palette entry bytes
        tokens.json          # mode-resolved v2 token document
        appearance.json      # metadata (contract appearance-artifact)
        <configDir>.css      # legacy palette CSS + role block + skin CSS
    current/appearance -> cache/appearance/<appearanceHash>/

Only ``current/appearance`` is repointed for an appearance change. The
``config/<configDir>/appearance.css`` convenience links are stable read paths
into that one hop.

The adapter owns all filesystem I/O. Generation is staged under
``cache/.staging-*``, every artifact is hashed, ``appearance.json`` is written
last, and the staging directory is atomically renamed into place. A failed
build leaves the old pointer untouched (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, cast

import fastjsonschema

from runtime.adapters.cache import cache_entry_path
from runtime.adapters.contract_schemas import (
    appearance_artifact_validator,
    appearance_json_validator,
)
from runtime.adapters.hashing import appearance_generation_hash, hash_file
from runtime.adapters.skin_expander import (
    MANIFEST_V2,
    load_skin_manifest,
    manifest_schema_version,
    render_v2_templates,
    resolve_v2_token_document,
    validate_v2_manifest,
)
from runtime.domain.models import (
    AppearanceEntry,
    PaletteEntry,
    SkinEntry,
    skin_config_dirs,
)
from runtime.domain.palette_vocabulary import PALETTE_VOCABULARY_V1

logger = logging.getLogger(__name__)

APPEARANCE_CACHE_DIR = "appearance"
APPEARANCE_POINTER_NAME = "appearance"
APPEARANCE_CONSUMER_NAME = "appearance.css"
APPEARANCE_TOKEN_REF = "tokens.json"
APPEARANCE_METADATA_NAME = "appearance.json"
APPEARANCE_COLORS_NAME = "colors.yaml"
DEFAULT_MODE = "dark"
APPEARANCE_KEEP = 5
APPEARANCE_GRACE_SECONDS = 900

_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_TOP_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*):(?:[ \t]+(.*?))?[ \t]*$")
#: Optional palette metadata that selects the mode. The palette producer is
#: not required to emit one; the fallback is documented (``dark``).
_MODE_KEYS = ("mode", "scheme", "appearance", "variant")
_DARK_BOOL_KEYS = ("is_dark", "dark")
_MODE_VALUES = frozenset({"dark", "light"})


def _is_hex64(value: str) -> bool:
    return bool(_HEX_RE.match(value))


def _cache_dir(state_root: Path, entry_hash: str) -> Path:
    return appearance_cache_root(state_root) / entry_hash


def appearance_cache_root(state_root: Path) -> Path:
    """Return ``state_root/cache/appearance`` (the v2 generation root)."""
    return state_root / "cache" / APPEARANCE_CACHE_DIR


def current_appearance_link(state_root: Path) -> Path:
    """Return the one stable appearance pointer path."""
    return state_root / "current" / APPEARANCE_POINTER_NAME


def _parse_scalar(raw: str) -> str | None:
    text = raw.strip()
    if not text or text.startswith("#"):
        return None
    if text[0] in ('"', "'"):
        end = text.find(text[0], 1)
        return text[1:end] if end != -1 else None
    if text[0] in ("|", ">", "[", "{", "&", "*", "!", "@", "`"):
        return None
    token = text.split(None, 1)[0]
    return token or None


def parse_colors_yaml(text: str) -> dict[str, str]:
    """Parse the top-level scalar subset of ``colors.yaml``.

    The runtime deliberately has no YAML dependency (``palette_conformance``
    uses the same subset): top-level ``key: value`` scalars only. The legacy
    ``colors:`` list is skipped by construction.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line[0] in (" ", "\t"):
            continue
        match = _TOP_KEY_RE.match(raw_line.strip())
        if match is None:
            continue
        value = _parse_scalar(match.group(2) or "")
        if value is not None:
            values[match.group(1)] = value
    return values


def palette_declares_mode(colors_yaml: str) -> bool:
    """True when the palette carries an explicit mode selector field.

    Used to distinguish a mode-aware palette (tpr >= 0.6.0 emits ``mode``)
    from legacy/fixture palettes with no mode metadata: only the former can
    participate in a mode-driven cache-key invalidation on reconcile.
    """
    values = parse_colors_yaml(colors_yaml)
    return any(key in values for key in _MODE_KEYS) or any(
        key in values for key in _DARK_BOOL_KEYS
    )


def detect_palette_mode(colors_yaml: str) -> str:
    """Derive the appearance mode from the generated palette.

    Precedence is deterministic and recorded in the manifest:

    1. a ``mode``/``scheme``/``appearance``/``variant`` scalar with a
       ``dark`` or ``light`` value;
    2. an ``is_dark``/``dark`` boolean scalar (``true`` → dark, ``false`` →
       light);
    3. ``dark``. The current producer does not emit a mode field; the
       fallback is documented in ``docs/ARCHITECTURE.md`` and tested.

    Mode is part of the appearance identity, so this function is the single
    selector used by generation, inspect and doctor.
    """
    values = parse_colors_yaml(colors_yaml)
    for key in _MODE_KEYS:
        raw = values.get(key)
        if raw is None:
            continue
        value = raw.strip().lower()
        if value in _MODE_VALUES:
            return value
    for key in _DARK_BOOL_KEYS:
        raw = values.get(key)
        if raw is None:
            continue
        value = raw.strip().lower()
        if value == "true":
            return "dark"
        if value == "false":
            return "light"
    return DEFAULT_MODE


def _number(value: object, default: float) -> int | float:
    """Return a unitless non-negative number from a v1 token value."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        number = float(value)
        return int(value) if number.is_integer() else number
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match is not None:
            number = float(match.group(0))
            if number < 0:
                return default
            return int(number) if number.is_integer() else number
    return default


def _text(value: object, default: str) -> str:
    return value if isinstance(value, str) and value.strip() else default


def _v1_group(skin_json: dict[str, Any], name: str) -> dict[str, Any]:
    group = skin_json.get(name)
    return cast("dict[str, Any]", group) if isinstance(group, dict) else {}


def tokens_from_v1_skin(skin_json: dict[str, Any]) -> dict[str, Any]:
    """Translate the frozen v1 skin token map to the v2 token document.

    This is the explicit RFC §5.2 compatibility mapping for the migration
    window, not a silent reinterpretation: the v1 skin cache remains the
    authoritative expansion and this function only shapes the resolved token
    document the appearance plane exposes. v2 manifests bypass it entirely.
    """
    shape = _v1_group(skin_json, "shape")
    surface = _v1_group(skin_json, "surface")
    ink = _v1_group(skin_json, "ink")
    typ = _v1_group(skin_json, "type")
    rhythm = _v1_group(skin_json, "rhythm")
    layout = _v1_group(skin_json, "layout")

    body = _number(typ.get("text-size"), 13)
    title = _number(typ.get("title-size"), 12.5)
    font_weight = _number(typ.get("font-weight"), 400)
    cell_pad = _number(rhythm.get("cell-pad"), 8)
    border = _text(surface.get("border"), "@outline")
    hairline = _number(shape.get("hairline-w"), 1)
    accent = _text(ink.get("accent"), "@primary")
    ink_value = _text(ink.get("ink"), "@on_surface")
    ink_dim = _text(ink.get("ink-dim"), "alpha(@on_surface, 0.62)")

    bar_layout = {
        key: _number(value, 0)
        for key, value in layout.items()
        if key not in ("variant", "structure", "order", "presence")
    }
    if not bar_layout:
        bar_layout = {"panel-width": 312}

    return {
        "version": 2,
        "surface": {
            "panel": {
                "bg": _text(surface.get("bg-panel"), "@surface"),
                "border": border,
                "border-width": _number(shape.get("border-w"), 1.5),
                "radius": _number(shape.get("radius-panel"), 12),
                "padding": cell_pad,
                "shadow": _text(surface.get("shadow"), "none"),
                "blur": _text(surface.get("blur"), "none"),
            },
            "card": {
                "bg": _text(surface.get("bg-card"), "@surface_container"),
                "border": border,
                "border-width": hairline,
                "radius": _number(shape.get("radius-card"), 9),
                "padding": cell_pad,
            },
            "cell": {
                "bg": "transparent",
                "border": "transparent",
                "border-width": 0,
                "radius": _number(shape.get("radius-cell"), 7),
                "padding": cell_pad,
                "hover-bg": "alpha(@on_surface, 0.08)",
            },
            "control": {
                "bg": "alpha(@surface_container, 0.45)",
                "border": border,
                "border-width": hairline,
                "radius": _number(shape.get("radius-cell"), 7),
                "padding": 4,
                "font-size": body,
                "font-weight": font_weight,
            },
            "pill": {
                "bg": "alpha(@on_surface, 0.12)",
                "border": border,
                "border-width": hairline,
                "radius": _number(shape.get("radius-pill"), 20),
                "padding": 2,
                "font-size": max(9, body - 3.5),
                "letter-spacing": 0.05,
            },
        },
        "state": {
            "hover": {
                "fill": "alpha(@on_surface, 0.08)",
                "ink": ink_value,
                "ring": accent,
            },
            "selected": {
                "fill": "@primary_container",
                "ink": "@on_primary_container",
                "ring": accent,
            },
            "focus": {"fill": "transparent", "ink": ink_value, "ring": accent},
            "disabled": {
                "fill": "transparent",
                "ink": ink_dim,
                "ring": "transparent",
            },
        },
        "type": {
            "font-family": _text(typ.get("font-family"), "inherit"),
            "font-weight": font_weight,
            "letter-spacing": 0.05,
            "size-caption": max(9, body - 2.5),
            "size-meta": max(9.5, body - 2),
            "size-body": body,
            "size-title": title,
            "size-display": title + 5,
        },
        "ink": {
            "ink": ink_value,
            "ink-dim": ink_dim,
            "accent": accent,
            "accent-muted": accent,
            "ink-over": "auto",
        },
        "layout": {"bar": bar_layout},
    }


def tokens_from_v2_manifest(manifest: dict[str, Any], mode: str) -> dict[str, Any]:
    """Resolve a v2 manifest for one mode (override replaces a whole group)."""
    return resolve_v2_token_document(manifest, mode)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a JSON object")
    return cast("dict[str, Any]", data)


def _load_v2_manifest(path: Path | None) -> dict[str, Any] | None:
    """Read a v2 manifest candidate (TOML or JSON); ``None`` for v1/absent."""
    if path is None or not path.is_file():
        return None
    try:
        data = load_skin_manifest(path)
        version = manifest_schema_version(data, path)
    except ValueError:
        return None
    # A v2 envelope is authoritative: malformed groups fail in
    # ``validate_v2_manifest`` instead of silently falling back to v1.
    return data if version == MANIFEST_V2 else None


def resolve_tokens(
    skin_entry: SkinEntry,
    skin_dir: Path,
    *,
    manifest_path: Path | None,
    mode: str,
) -> dict[str, Any]:
    """Resolve the v2 token document for this skin + mode, fail-closed."""
    manifest = _load_v2_manifest(manifest_path)
    if manifest is not None:
        assert manifest_path is not None
        validate_v2_manifest(manifest, skin_entry.name, manifest_path)
        tokens = tokens_from_v2_manifest(manifest, mode)
    else:
        skin_json = _load_json(skin_dir / "skin.json")
        tokens = tokens_from_v1_skin(skin_json)
    try:
        appearance_json_validator()(tokens)
    except fastjsonschema.JsonSchemaValueException as exc:
        raise ValueError(
            f"resolved tokens.json for skin {skin_entry.name!r} is not v2-conformant: "
            f"{exc.message}"
        ) from exc
    return tokens


def _render_palette_block(colors_yaml: str) -> str:
    """Render the legacy ``@define-color`` block from palette.v1 roles."""
    values = parse_colors_yaml(colors_yaml)
    missing = [role for role in PALETTE_VOCABULARY_V1 if role not in values]
    if missing:
        raise ValueError(
            "cannot render the appearance palette block; missing palette role(s): "
            + ", ".join(missing)
        )
    lines = [f"@define-color {role} {values[role]};" for role in PALETTE_VOCABULARY_V1]
    return "\n".join(lines) + "\n"


def _compose_css(palette_dir: Path, skin_css: str, colors_yaml: str) -> str:
    """Compose the single sheet the app applies.

    Order is frozen by the contract: legacy palette CSS, then the rendered
    palette role block, then the expanded skin CSS. The palette block is
    mandatory: without it ``@role`` references resolve to nothing.
    """
    legacy = (palette_dir / "colors.gtk.css").read_text(encoding="utf-8")
    role_block = _render_palette_block(colors_yaml)
    return (
        legacy.rstrip("\n")
        + "\n\n/* appearance: palette roles */\n"
        + role_block
        + "\n/* appearance: skin */\n"
        + skin_css.rstrip("\n")
        + "\n"
    )


def _generation_hash(root: Path, names: list[str]) -> str:
    entries = [(name, hash_file(root / name)) for name in sorted(names)]
    joined = "\x00".join(f"{rel}\x00{hexdigest}" for rel, hexdigest in entries)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _artifact_names(root: Path) -> list[str]:
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_file() and path.name != APPEARANCE_METADATA_NAME
    )


def load_appearance_entry(entry_dir: Path) -> AppearanceEntry:
    """Read and validate ``appearance.json`` from one generation."""
    data = _load_json(entry_dir / APPEARANCE_METADATA_NAME)
    try:
        appearance_artifact_validator()(data)
    except fastjsonschema.JsonSchemaValueException as exc:
        raise ValueError(
            f"{entry_dir / APPEARANCE_METADATA_NAME} is not a v2 appearance artifact: "
            f"{exc.message}"
        ) from exc
    appearance_hash = cast("str", data["appearanceHash"])
    if entry_dir.name != appearance_hash:
        raise ValueError(
            f"appearance directory {entry_dir.name!r} does not match its hash "
            f"{appearance_hash!r}"
        )
    files = _artifact_names(entry_dir)
    expected_generation = _generation_hash(entry_dir, files)
    if expected_generation != data["generationHash"]:
        raise ValueError(
            f"appearance generation hash mismatch in {entry_dir}: "
            f"manifest={data['generationHash']} actual={expected_generation}"
        )
    token_doc = _load_json(entry_dir / APPEARANCE_TOKEN_REF)
    try:
        appearance_json_validator()(token_doc)
    except fastjsonschema.JsonSchemaValueException as exc:
        raise ValueError(
            f"{entry_dir / APPEARANCE_TOKEN_REF} is not v2-conformant: {exc.message}"
        ) from exc
    config_dirs = tuple(
        sorted(name[: -len(".css")] for name in files if name.endswith(".css"))
    )
    return AppearanceEntry(
        version=2,
        appearance_hash=appearance_hash,
        palette_hash=cast("str", data["paletteHash"]),
        skin_hash=cast("str", data["skinHash"]),
        skin=cast("str", data["skin"]),
        mode=cast("Any", data["mode"]),
        generation_hash=cast("str", data["generationHash"]),
        token_ref=cast("Any", data["tokenRef"]),
        config_dirs=config_dirs,
    )


def read_current_appearance(state_root: Path) -> AppearanceEntry | None:
    """Read the live generation through ``current/appearance`` (read-only)."""
    link = current_appearance_link(state_root)
    try:
        resolved = link.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    try:
        return load_appearance_entry(resolved)
    except (OSError, ValueError):
        return None


def appearance_pointer_status(
    state_root: Path,
    expected_hash: str | None,
) -> tuple[str, str | None]:
    """Classify ``current/appearance`` against an expected generation.

    Returns ``(status, target)`` using the shared inspect/doctor vocabulary:
    ``ok``, ``missing``, ``diverged`` or ``dangling``.
    """
    link = current_appearance_link(state_root)
    if not link.is_symlink():
        return "missing", None
    if not link.exists():
        return "dangling", str(link.readlink())
    try:
        actual = link.resolve()
    except OSError:
        return "dangling", str(link.readlink())
    if expected_hash is not None:
        expected_dir = _cache_dir(state_root, expected_hash).resolve()
        if actual != expected_dir:
            return "diverged", str(actual)
    return "ok", str(actual)


def prune_appearance_entries(
    state_root: Path,
    *,
    keep: int = APPEARANCE_KEEP,
    grace_seconds: int = APPEARANCE_GRACE_SECONDS,
) -> list[Path]:
    """Remove old generations under the keep-N + active + grace policy.

    The active generation (the ``current/appearance`` target), the newest
    ``keep`` entries, and entries younger than ``grace_seconds`` are
    protected. Entries that cannot be classified are left alone.
    """
    if keep < 0:
        raise ValueError(f"keep must be >= 0, got {keep}")
    root = appearance_cache_root(state_root)
    if not root.is_dir() or root.is_symlink():
        return []
    active: set[Path] = set()
    link = current_appearance_link(state_root)
    if link.is_symlink():
        try:
            active.add(link.resolve())
        except OSError:
            pass
    entries: list[Path] = []
    for child in root.iterdir():
        if child.is_symlink() or not child.is_dir() or not _is_hex64(child.name):
            continue
        entries.append(child)
    entries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    protected = set(entries[:keep]) | active
    now = time.time()
    removed: list[Path] = []
    for entry in entries:
        if entry in protected:
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < grace_seconds:
            continue
        shutil.rmtree(entry, ignore_errors=False)
        removed.append(entry)
    return removed


def prune_stale_appearance_consumers(install_spine: Path, wanted: set[Path]) -> list[Path]:
    """Remove runtime-owned ``appearance.css`` links no longer published.

    Scope is deliberately narrow: only a symlink named ``appearance.css``
    under an existing ``<install>/config/*`` dir whose raw target names
    ``current/appearance``. Non-appearance files and user links are kept.
    """
    config_root = install_spine / "config"
    if not config_root.is_dir():
        return []
    removed: list[Path] = []
    for config_dir in sorted(config_root.iterdir()):
        if not config_dir.is_dir() or config_dir.is_symlink():
            continue
        link = config_dir / APPEARANCE_CONSUMER_NAME
        if link in wanted or not link.is_symlink():
            continue
        try:
            raw = os.readlink(link)
        except OSError:
            continue
        if f"current/{APPEARANCE_POINTER_NAME}" not in raw.replace("\\", "/"):
            continue
        try:
            link.unlink()
        except OSError:
            continue
        logger.info("appearance: pruned stale convenience link %s", link)
        removed.append(link)
    return removed


class AppearanceGenerator:
    """Compose and publish one immutable appearance cache entry."""

    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root

    def ensure(
        self,
        *,
        palette_entry: PaletteEntry,
        skin_entry: SkinEntry,
        install_spine: Path,
        manifest_path: Path | None = None,
        templates_dir: Path | None = None,
        keep: int = APPEARANCE_KEEP,
    ) -> tuple[AppearanceEntry, bool]:
        palette_dir = cache_entry_path(
            self._state_root, "palettes", palette_entry.entry_hash
        )
        skin_dir = cache_entry_path(self._state_root, "skins", skin_entry.entry_hash)
        colors_path = palette_dir / APPEARANCE_COLORS_NAME
        if not colors_path.is_file():
            raise ValueError(f"appearance palette artifact missing: {colors_path}")
        if not skin_dir.is_dir():
            raise ValueError(f"appearance skin entry missing: {skin_dir}")
        colors_yaml = colors_path.read_text(encoding="utf-8")
        mode = detect_palette_mode(colors_yaml)
        appearance_hash = appearance_generation_hash(
            palette_entry.entry_hash, skin_entry.entry_hash, mode
        )
        target = self._cache_entry_dir(appearance_hash)
        if target.is_dir():
            try:
                entry = load_appearance_entry(target)
            except (OSError, ValueError) as exc:
                logger.warning("appearance: evicting invalid cache entry %s: %s", target, exc)
                shutil.rmtree(target, ignore_errors=False)
            else:
                prune_appearance_entries(self._state_root, keep=keep)
                return entry, True

        config_dirs = skin_config_dirs(skin_entry.artifact_hashes)
        if not config_dirs:
            raise ValueError(f"appearance skin {skin_entry.name!r} has no <configDir>.css targets")
        manifest = _load_v2_manifest(manifest_path)
        rendered: dict[str, str] | None = None
        if manifest is not None:
            # v2 templates must be expanded AFTER the mode override is applied.
            # The skin cache's base render is migration-only and never composed.
            assert manifest_path is not None
            validate_v2_manifest(manifest, skin_entry.name, manifest_path)
            tokens = tokens_from_v2_manifest(manifest, mode)
            if templates_dir is None or not templates_dir.is_dir():
                raise ValueError(
                    "appearance v2 manifest requires a skin templates dir to "
                    f"expand mode={mode!r}: {templates_dir}"
                )
            rendered = render_v2_templates(templates_dir, tokens)
        else:
            tokens = resolve_tokens(
                skin_entry, skin_dir, manifest_path=manifest_path, mode=mode
            )

        root = appearance_cache_root(self._state_root)
        root.mkdir(parents=True, exist_ok=True)
        staging = self._state_root / "cache" / (
            f".staging-{os.getpid()}-{uuid.uuid4().hex}"
        )
        staging.mkdir(parents=True, exist_ok=False)
        try:
            shutil.copyfile(colors_path, staging / APPEARANCE_COLORS_NAME)
            (staging / APPEARANCE_TOKEN_REF).write_text(
                json.dumps(tokens, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            for config_dir in config_dirs:
                if rendered is not None:
                    skin_css = rendered.get(f"{config_dir}.css")
                    if skin_css is None:
                        raise ValueError(
                            f"appearance skin {skin_entry.name!r} has no rendered "
                            f"{config_dir}.css"
                        )
                else:
                    try:
                        skin_css = (skin_dir / f"{config_dir}.css").read_text(
                            encoding="utf-8"
                        )
                    except (OSError, UnicodeDecodeError) as exc:
                        raise ValueError(
                            f"appearance CSS composition failed for {config_dir!r}: {exc}"
                        ) from exc
                try:
                    css = _compose_css(palette_dir, skin_css, colors_yaml)
                except (OSError, UnicodeDecodeError) as exc:
                    raise ValueError(
                        f"appearance CSS composition failed for {config_dir!r}: {exc}"
                    ) from exc
                (staging / f"{config_dir}.css").write_text(css, encoding="utf-8")
            generation_hash = _generation_hash(staging, _artifact_names(staging))
            manifest = {
                "version": 2,
                "skin": skin_entry.name,
                "mode": mode,
                "paletteHash": palette_entry.entry_hash,
                "skinHash": skin_entry.entry_hash,
                "appearanceHash": appearance_hash,
                "generationHash": generation_hash,
                "tokenRef": APPEARANCE_TOKEN_REF,
            }
            try:
                appearance_artifact_validator()(manifest)
            except fastjsonschema.JsonSchemaValueException as exc:
                raise ValueError(f"generated appearance.json is invalid: {exc.message}") from exc
            # The manifest is written LAST: a partially written generation is
            # never mistaken for a complete one.
            (staging / APPEARANCE_METADATA_NAME).write_text(
                json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            try:
                os.rename(staging, target)
            except OSError:
                if not target.is_dir():
                    raise
                # Lost a race: the winner's immutable entry is authoritative.
                return load_appearance_entry(target), True
            prune_appearance_entries(self._state_root, keep=keep)
            return load_appearance_entry(target), False
        finally:
            try:
                shutil.rmtree(staging, ignore_errors=True)
            except OSError:
                pass

    def _cache_entry_dir(self, entry_hash: str) -> Path:
        # ``cache/appearance`` is intentionally not one of the CACHE_LAYERS
        # (it is a composed plane, not a derivation layer), so validate the
        # hex name here instead of calling cache_entry_path.
        if not _is_hex64(entry_hash):
            raise ValueError(f"appearance hash must be 64-char hex, got {entry_hash!r}")
        return appearance_cache_root(self._state_root) / entry_hash


def expected_mode(state_root: Path, palette_entry: PaletteEntry | None) -> str:
    """Resolve the mode for a recorded palette (read-only, for inspect/doctor)."""
    if palette_entry is not None:
        colors_path = (
            cache_entry_path(state_root, "palettes", palette_entry.entry_hash)
            / APPEARANCE_COLORS_NAME
        )
        try:
            return detect_palette_mode(colors_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            pass
    return DEFAULT_MODE
