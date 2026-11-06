"""Skin manifest parser + deterministic template expander (FROZEN SKIN SPEC v1).

The adapter owns all filesystem reads/writes for the skin layer. The frozen
scope is a palette/ink mapping + paint + rhythm: no structure or variant
tokens, and no Jinja runtime — expansion is the literal ``{{SKIN_<TOKEN>}}``
substitution the spec pins.

Manifest v1 (JSON or TOML), one per skin under ``skins/manifests/``::

    schema_major = 1
    name = "glass"

    [shape]
    radius-panel = 12
    radius-card = 9
    radius-cell = 7
    radius-pill = 20
    border-w = 1.5

    [rhythm]
    gap = 8
    cell-pad = "8px 9px"

    [surface]
    bg-panel = "alpha(@surface, 0.94)"
    bg-card = "alpha(@surface_container_high, 0.18)"
    border = "@primary"
    shadow = "none"
    blur = "none"

    [ink]
    ink = "@on_surface"
    ink-dim = "alpha(@on_surface, 0.62)"
    accent = "@primary"
    ink-over = "auto"

    [type]
    font-family = "inherit"
    title-size = 12.5
    text-size = 13

    [layout]
    panel-width = 312
    bt-list-spacing = 4
    bar-row-height = 0
    chip-min = 0

There is no ``[tokens]`` table, no ``[apps]`` table and no structure/variant
key. Dimensions are unitless numbers; a unit string in a manifest (for
example ``radius-panel = "12px"``) is rejected. ``skin.json`` never carries
units.

The shared template set holds one ``<configDir>.gtk.css.j2`` per target config
dir. The expander discovers every ``*.gtk.css.j2`` and renders
``<configDir>.css``. A placeholder is the token name uppercased with every
non-alphanumeric run replaced by ``_``: ``radius-panel`` →
``{{SKIN_RADIUS_PANEL}}``. Unknown placeholders and unresolved placeholders
fail closed.

In CSS a numeric token renders as ``<number>px`` (``12`` → ``12px``, ``1.5`` →
``1.5px``); ``cell-pad`` and every string token render verbatim. ``skin.json``
is the grouped token map ``{"version": 1, "<group>": {...}}`` with numeric
tokens stored as raw numbers, which is the shape ``parseSkinJson`` accepts.

Validation is fail-closed: unknown keys, unsafe paths, non-scalar tokens and
unresolved placeholders raise ``ValueError`` naming the manifest/template and
nothing is written outside the entry work dir.
"""

from __future__ import annotations

import json
import math
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from runtime.adapters.hashing import hash_file, skin_entry_hash
from runtime.domain.models import SkinArtifacts, SkinEntry
from runtime.ports.skin_expander import ISkinExpander

#: Canonical group order (the ``skin.json`` / ``parseSkinJson`` group set).
_TOKEN_GROUPS: dict[str, tuple[str, ...]] = {
    "shape": (
        "radius-panel",
        "radius-card",
        "radius-cell",
        "radius-pill",
        "border-w",
    ),
    "surface": ("bg-panel", "bg-card", "border", "shadow", "blur"),
    "ink": ("ink", "ink-dim", "accent", "ink-over"),
    "type": ("font-family", "title-size", "text-size"),
    "rhythm": ("gap", "cell-pad"),
    "layout": ("panel-width", "bt-list-spacing", "bar-row-height", "chip-min"),
}

#: Tokens rendered as ``<number>px`` and stored as raw numbers in ``skin.json``.
_NUMERIC_TOKENS = frozenset(
    {
        "radius-panel",
        "radius-card",
        "radius-cell",
        "radius-pill",
        "border-w",
        "gap",
        "title-size",
        "text-size",
        "panel-width",
        "bt-list-spacing",
        "bar-row-height",
        "chip-min",
    }
)

_MANIFEST_META_KEYS = frozenset({"schema_major", "name"})
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TEMPLATE_SUFFIX = ".gtk.css.j2"
_PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)

Scalar = str | int | float


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _token_placeholder(key: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").upper()
    return f"SKIN_{normalized}"


def _format_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if value.is_integer():
        return str(int(value))
    return format(value, "g")


def _parse_number(value: Any, *, token: str, path: Path) -> int | float:
    """Return a canonical unitless number; reject bools, units and negatives."""
    if isinstance(value, bool):
        raise ValueError(f"skin token {token!r} must be a number, not a bool: {path}")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"skin token {token!r} must be finite and non-negative: {path}")
        return int(value) if isinstance(value, int) or number.is_integer() else number
    raise ValueError(
        f"skin token {token!r} must be a unitless number, got {value!r}: {path}"
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read skin manifest {path}: {exc}") from exc
    try:
        if path.suffix == ".json":
            data = json.loads(raw)
        elif path.suffix == ".toml":
            data = tomllib.loads(raw)
        else:
            raise ValueError(
                f"skin manifest must be .json or .toml, got {path.suffix!r}: {path}"
            )
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(
            f"skin manifest is not valid {path.suffix.lstrip('.')}: {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"skin manifest must be an object: {path}")
    return cast("dict[str, Any]", data)


def _validate_manifest(
    data: dict[str, Any], selected_name: str, path: Path
) -> dict[str, dict[str, Scalar]]:
    unknown = set(data) - _MANIFEST_META_KEYS - set(_TOKEN_GROUPS)
    if unknown:
        raise ValueError(f"skin manifest has unknown keys {sorted(unknown)}: {path}")

    name = data.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"skin manifest 'name' must be a safe lowercase name: {path}")
    if name != selected_name:
        raise ValueError(
            f"skin manifest name {name!r} does not match selected skin {selected_name!r}: {path}"
        )

    schema_major = data.get("schema_major")
    if type(schema_major) is not int or schema_major != 1:
        raise ValueError(
            f"skin manifest 'schema_major' must be 1, got {schema_major!r}: {path}"
        )

    groups: dict[str, dict[str, Scalar]] = {}
    for group, tokens in _TOKEN_GROUPS.items():
        raw_group = data.get(group)
        if not isinstance(raw_group, dict):
            raise ValueError(
                f"skin manifest group {group!r} must be a table/object: {path}"
            )
        unknown_tokens = set(raw_group) - set(tokens)
        missing_tokens = set(tokens) - set(raw_group)
        if unknown_tokens:
            raise ValueError(
                f"skin manifest group {group!r} has unknown token(s) "
                f"{sorted(unknown_tokens)}: {path}"
            )
        if missing_tokens:
            raise ValueError(
                f"skin manifest group {group!r} is missing token(s) "
                f"{sorted(missing_tokens)}: {path}"
            )
        values: dict[str, Scalar] = {}
        for token in tokens:
            value = raw_group[token]
            if token in _NUMERIC_TOKENS:
                values[token] = _parse_number(value, token=token, path=path)
            else:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"skin token {group}.{token} must be a non-empty string: {path}"
                    )
                values[token] = value
        groups[group] = values
    return groups


def _css_value(token: str, value: Scalar) -> str:
    if token in _NUMERIC_TOKENS:
        return f"{_format_number(cast('int | float', value))}px"
    return cast("str", value)


def _placeholder_values(groups: dict[str, dict[str, Scalar]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for group, tokens in _TOKEN_GROUPS.items():
        for token in tokens:
            placeholder = _token_placeholder(token)
            if placeholder in values:
                raise ValueError(f"skin tokens collide on placeholder {placeholder!r}")
            values[placeholder] = _css_value(token, groups[group][token])
    return values


def _discover_templates(templates_dir: Path) -> list[tuple[Path, str]]:
    try:
        candidates = sorted(
            path
            for path in templates_dir.iterdir()
            if path.is_file() and path.name.endswith(_TEMPLATE_SUFFIX)
        )
    except OSError as exc:
        raise ValueError(f"cannot read skin templates dir {templates_dir}: {exc}") from exc
    if not candidates:
        raise ValueError(
            f"skin templates dir has no '*{_TEMPLATE_SUFFIX}' files: {templates_dir}"
        )
    templates: list[tuple[Path, str]] = []
    outputs: set[str] = set()
    for path in candidates:
        config_dir = path.name[: -len(_TEMPLATE_SUFFIX)]
        if not _NAME_RE.match(config_dir):
            raise ValueError(
                f"skin template {path.name!r} has an unsafe config dir {config_dir!r}"
            )
        output = f"{config_dir}.css"
        if output in outputs:
            raise ValueError(f"skin templates collide on output {output!r}")
        outputs.add(output)
        templates.append((path, output))
    return templates


def _substitute(text: str, values: dict[str, str], template: Path) -> str:
    def _replace(match: re.Match[str]) -> str:
        placeholder = match.group(1).strip()
        if placeholder not in values:
            raise ValueError(
                f"skin template {template} references unknown placeholder "
                f"{{{{{placeholder}}}}}"
            )
        return values[placeholder]

    rendered = _PLACEHOLDER_RE.sub(_replace, text)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError(f"skin template {template} has an unresolved placeholder")
    return rendered


class SkinExpander(ISkinExpander):
    """Deterministic JSON/TOML manifest expansion into a cache work dir."""

    def expand(
        self,
        name: str,
        manifest_path: Path,
        templates_dir: Path,
        output_dir: Path,
    ) -> SkinEntry:
        if not _NAME_RE.match(name):
            raise ValueError(f"skin name must be a safe lowercase name, got {name!r}")
        if not templates_dir.is_dir():
            raise ValueError(f"skin templates dir not found: {templates_dir}")
        manifest = _load_manifest(manifest_path)
        groups = _validate_manifest(manifest, name, manifest_path)
        values = _placeholder_values(groups)
        templates = _discover_templates(templates_dir)

        templates_hash = _canonical_templates_hash(templates_dir)
        manifest_hash = hash_file(manifest_path)
        expected = skin_entry_hash(templates_hash, manifest_hash)
        if output_dir.name != expected:
            raise ValueError(
                f"skin output dir must be named {expected}, got {output_dir.name!r}"
            )

        rendered: dict[str, str] = {}
        for template, output in templates:
            try:
                source = template.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError(f"cannot read skin template {template}: {exc}") from exc
            rendered[output] = _substitute(source, values, template)

        token_map = json.dumps(
            {"version": 1, **groups}, indent=2, ensure_ascii=False
        ) + "\n"
        output_dir.mkdir(parents=True, exist_ok=False)
        try:
            for filename, text in sorted(rendered.items()):
                (output_dir / filename).write_text(text, encoding="utf-8")
            (output_dir / "skin.json").write_text(token_map, encoding="utf-8")
            artifact_hashes: SkinArtifacts = {
                filename: hash_file(output_dir / filename)
                for filename in (*rendered.keys(), "skin.json")
            }
        except BaseException:
            # The staging owner removes partial work; never leave it visible.
            raise
        return SkinEntry(
            hash_algorithm="sha256",
            kind="skin",
            entry_hash=expected,
            name=name,
            input_templates_hash=templates_hash,
            input_manifest_hash=manifest_hash,
            artifact_hashes=artifact_hashes,
            generated_at=_now_z(),
        )


def _canonical_templates_hash(templates_dir: Path) -> str:
    # Lazy import keeps the adapter module's public surface small and avoids a
    # cycle with hashing's appearance helpers.
    from runtime.adapters.hashing import canonical_hash_dir

    return canonical_hash_dir(templates_dir)
