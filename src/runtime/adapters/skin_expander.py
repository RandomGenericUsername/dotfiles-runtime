"""Skin manifest parser + deterministic template expander (v1 + v2).

The adapter owns all filesystem reads/writes for the skin layer. Two manifest
generations are accepted:

* **v1** (``schema_major = 1``): the frozen flat groups ``shape``,
  ``surface``, ``ink``, ``type``, ``rhythm`` and ``layout``.
* **v2** (``schemaVersion = 2``): the ``contracts v0.9.0`` appearance model with
  nested ``[groups.surface.<role>]`` / ``[groups.state.<state>]`` /
  ``[groups.type]`` / ``[groups.ink]`` / ``[groups.layout.<app>]`` and optional
  ``[mode.dark]`` / ``[mode.light]`` complete same-shape overrides. v2
  documents are validated against the vendored ``appearance-manifest`` schema.

The shared template set holds one ``<configDir>.gtk.css.j2`` per CSS target
config dir plus optional non-CSS artifacts. Discovery maps a suffix to
``(family, format, output)``: ``.gtk.css.j2`` → CSS target, ``.rasi.j2`` →
``rasi`` artifact, ``.conf.j2`` → ``conf`` artifact and ``.theme.j2`` →
``theme`` artifact. Any other ``*.j2`` file fails closed.

Placeholders are the token path uppercased with every non-alphanumeric run
replaced by ``_``: v1 ``radius-panel`` → ``{{SKIN_RADIUS_PANEL}}``; v2
``surface.panel.bg`` → ``{{SKIN_SURFACE_PANEL_BG}}``. v2 lengths render
``<number>px``, ``letter-spacing`` renders ``<number>em``,
``surface.window.opacity`` renders a bare unitless number and ``font-weight``
stays unitless. ``conf`` artifacts render lengths as bare points (``value *
0.75``, round half up to two decimals, clamped per option), project the first
usable font family and never emit a ``px``/``pt`` suffix. Unknown/unresolved
placeholders, layout placeholders in CSS and conditional syntax fail closed.

Validation is fail-closed: unknown keys, unsafe paths, non-scalar tokens and
unresolved placeholders raise ``ValueError`` naming the manifest/template and
nothing is written outside the entry work dir.
"""

from __future__ import annotations

import copy
import json
import math
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal, cast

import fastjsonschema

from runtime.adapters.contract_schemas import (
    appearance_json_validator,
    appearance_manifest_validator,
)
from runtime.adapters.hashing import hash_file, skin_entry_hash
from runtime.domain.host_defaults import (
    HostResolution,
    HostSnapshot,
    resolve_host_defaults,
)
from runtime.domain.models import SkinArtifacts, SkinEntry
from runtime.ports.skin_expander import ISkinExpander

#: Manifest generations. v1 stays readable until the assets tuple bump.
MANIFEST_V1 = 1
MANIFEST_V2 = 2

#: v2 top-level groups (the resolved document's keys plus ``version``).
_V2_GROUPS = ("surface", "state", "type", "ink", "layout")
#: v2 groups a template may expand. ``layout.<app>`` is TSX-owned, never CSS.
_V2_CSS_GROUPS = ("surface", "state", "type", "ink")
_V2_MODES = frozenset({"dark", "light"})

#: Canonical v1 group order (the ``skin.json`` / ``parseSkinJson`` group set).
_TOKEN_GROUPS: dict[str, tuple[str, ...]] = {
    "shape": (
        "radius-panel",
        "radius-card",
        "radius-cell",
        "radius-pill",
        "border-w",
        "hairline-w",
    ),
    "surface": ("bg-panel", "bg-card", "border", "shadow", "blur"),
    "ink": ("ink", "ink-dim", "accent", "ink-over"),
    "type": ("font-family", "title-size", "text-size"),
    "rhythm": ("gap", "cell-pad"),
    "layout": ("panel-width", "bt-list-spacing", "bar-row-height", "chip-min"),
}

#: v1 tokens rendered as ``<number>px`` and stored as raw numbers in ``skin.json``.
_NUMERIC_TOKENS = frozenset(
    {
        "radius-panel",
        "radius-card",
        "radius-cell",
        "radius-pill",
        "border-w",
        "hairline-w",
        "gap",
        "title-size",
        "text-size",
        "panel-width",
        "bt-list-spacing",
        "bar-row-height",
        "chip-min",
    }
)

_V1_MANIFEST_META_KEYS = frozenset({"schema_major", "name"})
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TEMPLATE_SUFFIX = ".gtk.css.j2"
_ARTIFACT_SUFFIXES: dict[str, Literal["rasi", "conf", "theme"]] = {
    ".rasi.j2": "rasi",
    ".conf.j2": "conf",
    ".theme.j2": "theme",
}
_CONSUMER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_ROLE_REF_RE = re.compile(r"@([a-z0-9_]+)")
_ALPHA_RE = re.compile(r"alpha\(\s*(@[a-z0-9_]+)\s*,[^)]*\)")
_PAINT_UNREPRESENTABLE = "\x00SKIN_PAINT_UNREPRESENTABLE:"
#: v2 templates are unconditional (contracts v0.9.0). GTK has no runtime.
_CONDITIONAL_RE = re.compile(r"\{%|%\}|\{\{#|\{\{/")

#: ``conf`` lengths render as bare points (CSS reference pixel, docs/113 §7.3).
_CONF_PT_SCALE = Decimal("0.75")
_CONF_PT_QUANTUM = Decimal("0.01")
#: Kitty presentation clamps: font_size 6–36, window_padding_width 0–32.
_CONF_PT_CLAMPS: dict[str, tuple[Decimal, Decimal]] = {
    "type.size-body": (Decimal("6"), Decimal("36")),
    "surface.window.padding": (Decimal("0"), Decimal("32")),
}
_CONF_CSS_WIDE_FAMILIES = frozenset(
    {"inherit", "initial", "unset", "revert", "revert-layer"}
)
_CONF_NON_PORTABLE_FAMILIES = frozenset(
    {"system-ui", "ui-sans-serif", "ui-monospace", "ui-serif"}
)
_CONF_FALLBACK_FONT_FAMILY = "monospace"
_CSS_ESCAPE_RE = re.compile(r"\\(.)")

Scalar = str | int | float
TemplateFormat = Literal["css", "rasi", "conf", "theme"]
TemplateFamily = Literal["targets", "artifacts"]


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    """One discovered template and the manifest family it publishes."""

    path: Path
    family: TemplateFamily
    format: TemplateFormat
    consumer: str
    output: str


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _flatten_placeholder(path: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_").upper()
    return f"SKIN_{normalized}"


def _token_placeholder(key: str) -> str:
    """v1 token name -> placeholder (``radius-panel`` -> ``SKIN_RADIUS_PANEL``)."""
    return _flatten_placeholder(key)


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


def load_skin_manifest(path: Path) -> dict[str, Any]:
    """Read a JSON or TOML skin manifest as an object."""
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


def manifest_schema_version(data: dict[str, Any], path: Path) -> int:
    """Return 1 for ``schema_major`` or 2 for ``schemaVersion``; reject ambiguity."""
    has_v1 = "schema_major" in data
    has_v2 = "schemaVersion" in data
    if has_v1 and has_v2:
        raise ValueError(
            "skin manifest declares both 'schema_major' (v1) and "
            f"'schemaVersion' (v2): {path}"
        )
    if has_v1:
        return MANIFEST_V1
    if has_v2:
        return MANIFEST_V2
    raise ValueError(
        f"skin manifest must declare 'schema_major' (v1) or 'schemaVersion' (v2): {path}"
    )


def _validate_v1_manifest(
    data: dict[str, Any], selected_name: str, path: Path
) -> dict[str, dict[str, Scalar]]:
    unknown = set(data) - _V1_MANIFEST_META_KEYS - set(_TOKEN_GROUPS)
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


def validate_v2_manifest(
    data: dict[str, Any], selected_name: str, path: Path
) -> dict[str, Any]:
    """Validate a v2 appearance manifest against the vendored canonical schema."""
    try:
        appearance_manifest_validator()(data)
    except fastjsonschema.JsonSchemaValueException as exc:
        raise ValueError(
            f"skin manifest is not a v2 appearance manifest: {exc.message}: {path}"
        ) from exc
    name = data.get("name")
    if name != selected_name:
        raise ValueError(
            f"skin manifest name {name!r} does not match selected skin {selected_name!r}: {path}"
        )
    return data


def _resolve_v2_base_tokens(
    manifest: dict[str, Any], mode: str | None
) -> dict[str, Any]:
    """Resolve base v2 groups plus one mode override (whole-group replace)."""
    groups = manifest.get("groups")
    if not isinstance(groups, dict):
        raise ValueError("appearance manifest has no 'groups' object")
    resolved = copy.deepcopy(groups)
    if mode is not None:
        if mode not in _V2_MODES:
            raise ValueError(f"appearance mode must be 'dark' or 'light', got {mode!r}")
        overrides = manifest.get("mode")
        if isinstance(overrides, dict):
            selected = overrides.get(mode)
            if selected is not None:
                if not isinstance(selected, dict):
                    raise ValueError(f"appearance mode.{mode} override must be an object")
                resolved.update(copy.deepcopy(selected))
    return {"version": 2, **resolved}


def resolve_v2_token_document_with_host(
    manifest: dict[str, Any],
    mode: str | None,
    host: HostSnapshot | None = None,
) -> tuple[dict[str, Any], HostResolution]:
    """Resolve mode groups, then the skin's optional host policy.

    The skin cache's authored-base render passes ``host=None``; generation
    passes the one snapshot read inside ``converge.lock``. Host values are
    applied after mode selection, so a complete selected mode group wins.
    """
    base = _resolve_v2_base_tokens(manifest, mode)
    resolution = resolve_host_defaults(base, manifest, mode, host)
    tokens = resolution.tokens
    try:
        appearance_json_validator()(tokens)
    except fastjsonschema.JsonSchemaValueException as exc:
        raise ValueError(
            f"resolved v2 tokens document is not appearance-json conformant: {exc.message}"
        ) from exc
    return tokens, resolution


def resolve_v2_token_document(
    manifest: dict[str, Any],
    mode: str | None,
    host: HostSnapshot | None = None,
) -> dict[str, Any]:
    """Resolve base v2 groups + one mode override + optional host policy."""
    tokens, _resolution = resolve_v2_token_document_with_host(manifest, mode, host)
    return tokens


def _leaves(value: Any, prefix: str) -> list[tuple[str, Any]]:
    leaves: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            leaves.extend(_leaves(child, path))
    else:
        leaves.append((prefix, value))
    return leaves


def _css_v2_value(path: str, value: Any) -> str:
    """Render one v2 token in its CSS position (contracts v0.12.0 unit rule)."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        number = _format_number(value)
        if path == "surface.window.opacity":
            # Unitless [0,1] toplevel opacity, authored and never an alpha()
            # derivation. It is not a length, so no px suffix.
            return number
        if path.endswith("letter-spacing"):
            return f"{number}em"
        if path.endswith("font-weight"):
            return number
        return f"{number}px"
    return cast("str", value)


def _format_conf_number(value: Decimal) -> str:
    """Render a decimal without an exponent or redundant trailing zeros."""
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


def _conf_pt_value(path: str, value: int | float) -> str:
    """Render one ``conf`` length as a bare point number (docs/113 §7.3).

    Kitty accepts a bare number only: ``px``/``pt`` suffixes are a parse
    error. The conversion is the CSS reference pixel (``1px = 0.75pt``), round
    half up to two decimals, then the per-option presentation clamp.
    """
    points = (Decimal(str(value)) * _CONF_PT_SCALE).quantize(
        _CONF_PT_QUANTUM, rounding=ROUND_HALF_UP
    )
    bounds = _CONF_PT_CLAMPS.get(path)
    if bounds is not None:
        low, high = bounds
        points = min(max(points, low), high)
    return _format_conf_number(points)


def _split_font_families(value: str) -> list[str]:
    """Split a CSS family list on commas outside quotes and escapes."""
    families: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            current.append(char)
            quote = char
            continue
        if char == ",":
            families.append("".join(current))
            current = []
            continue
        current.append(char)
    families.append("".join(current))
    return families


def _first_usable_font_family(value: str) -> str:
    """Project a CSS family list to the first kitty-usable family (docs/113 §7.4)."""
    for raw in _split_font_families(value):
        family = raw.strip()
        if len(family) >= 2 and family[0] == family[-1] and family[0] in ('"', "'"):
            family = family[1:-1]
        family = _CSS_ESCAPE_RE.sub(lambda match: match.group(1), family).strip()
        if not family:
            continue
        lowered = family.lower()
        if lowered in _CONF_CSS_WIDE_FAMILIES or lowered in _CONF_NON_PORTABLE_FAMILIES:
            continue
        return family
    return _CONF_FALLBACK_FONT_FAMILY


def _conf_value(path: str, value: Any) -> str:
    """Render one non-paint v2 token for a paint-free ``conf`` artifact."""
    if path == "type.font-family":
        return _first_usable_font_family(cast("str", value))
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        number = _format_number(value)
        if path.endswith(".opacity"):
            return number
        if path.endswith("letter-spacing"):
            return f"{number}em"
        if path.endswith("font-weight"):
            return number
        return _conf_pt_value(path, value)
    return cast("str", value)


def _is_paint_token(path: str) -> bool:
    """Whether a v2 token is paint (and therefore not representable in conf/theme)."""
    group, _, leaf = path.partition(".")
    if group == "surface":
        return leaf.rsplit(".", 1)[-1] in {"bg", "border", "shadow", "blur", "hover-bg"}
    if group == "state":
        return leaf.rsplit(".", 1)[-1] in {"fill", "ink", "ring"}
    return group == "ink"


def _rasi_value(path: str, value: Any) -> str:
    """Render a v2 token for rofi rasi: solid, hyphenated palette roles only."""
    text = _css_v2_value(path, value)
    if not isinstance(value, str) or not _is_paint_token(path):
        return text
    # rasi has no alpha(); the nearest honest solid is the referenced role.
    text = _ALPHA_RE.sub(lambda match: match.group(1), text)
    if "color-mix(" in text:
        raise ValueError(
            f"skin token {path!r} uses color-mix() which cannot be represented in rasi"
        )
    text = _ROLE_REF_RE.sub(
        lambda match: "@" + match.group(1).replace("_", "-"), text
    )
    if "#" in text:
        raise ValueError(f"skin token {path!r} is a literal hex paint, invalid in rasi")
    return text


def _format_v2_value(path: str, value: Any, format: TemplateFormat) -> str:
    """Render one v2 token for its consumer format.

    CSS intentionally delegates to the frozen ``_css_v2_value`` rule so every
    existing sheet stays byte-identical. ``rasi`` aliases palette roles with
    hyphens and never emits ``alpha()`` or hex. ``conf``/``theme`` are reserved
    shapes with no palette variables, so a paint token fails closed instead of
    becoming a false CSS/role claim.
    """
    if format == "css":
        return _css_v2_value(path, value)
    if format == "rasi":
        return _rasi_value(path, value)
    if _is_paint_token(path):
        # Defer the error until substitution: an unreferenced paint token is
        # harmless, while a template that actually uses it must fail closed.
        return f"{_PAINT_UNREPRESENTABLE}{path}\x00"
    if format == "conf":
        # ``conf`` is a bare-number format: lengths are points, never px/pt.
        return _conf_value(path, value)
    return _css_v2_value(path, value)


def _v2_placeholder_values(
    resolved: dict[str, Any],
    format: TemplateFormat = "css",
) -> tuple[dict[str, str], frozenset[str]]:
    """Flatten the CSS token leaves and record the forbidden layout placeholders."""
    values: dict[str, str] = {}
    for group in _V2_CSS_GROUPS:
        for path, value in _leaves(resolved.get(group, {}), group):
            placeholder = _flatten_placeholder(path)
            if placeholder in values:
                raise ValueError(f"skin tokens collide on placeholder {placeholder!r}")
            values[placeholder] = _format_v2_value(path, value, format)
    layout = frozenset(
        _flatten_placeholder(path)
        for path, _value in _leaves(resolved.get("layout", {}), "layout")
    )
    return values, layout


def expand_v2_template(
    text: str,
    resolved: dict[str, Any],
    *,
    template: Path | str = "<template>",
    format: TemplateFormat = "css",
) -> str:
    """Expand a v2 template with mode-resolved tokens, fail-closed."""
    values, layout = _v2_placeholder_values(resolved, format)
    return _substitute(
        text,
        values,
        template,
        layout_placeholders=layout,
        reject_conditionals=True,
        format=format,
    )


def render_v2_templates(
    templates_dir: Path, resolved: dict[str, Any]
) -> dict[str, str]:
    """Render every discovered v2 template with one resolved token document."""
    rendered: dict[str, str] = {}
    for template in discover_templates(templates_dir):
        try:
            source = template.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"cannot read skin template {template.path}: {exc}") from exc
        rendered[template.output] = expand_v2_template(
            source,
            resolved,
            template=template.path,
            format=template.format,
        )
    return rendered


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


def discover_templates(templates_dir: Path) -> tuple[TemplateSpec, ...]:
    """Map every template file to its family/format/output, fail-closed.

    Any ``*.j2`` file whose suffix is not in the frozen table is an error, not
    an ignored file: an assets template shipped ahead of the runtime would
    otherwise vanish silently. Consumers are unique across both families so a
    CSS target and a non-CSS artifact can never race for the same name.
    """
    try:
        candidates = sorted(path for path in templates_dir.iterdir() if path.is_file())
    except OSError as exc:
        raise ValueError(f"cannot read skin templates dir {templates_dir}: {exc}") from exc
    templates: list[TemplateSpec] = []
    consumers: dict[str, str] = {}
    outputs: set[str] = set()
    for path in candidates:
        name = path.name
        if not name.endswith(".j2"):
            continue
        if name.endswith(_TEMPLATE_SUFFIX):
            family: TemplateFamily = "targets"
            format: TemplateFormat = "css"
            suffix = _TEMPLATE_SUFFIX
        else:
            match = next(
                (candidate for candidate in _ARTIFACT_SUFFIXES if name.endswith(candidate)),
                None,
            )
            if match is None:
                raise ValueError(
                    f"skin template {name!r} has an unknown suffix; expected "
                    f"{_TEMPLATE_SUFFIX!r}, {sorted(_ARTIFACT_SUFFIXES)}"
                )
            family = "artifacts"
            format = _ARTIFACT_SUFFIXES[match]
            suffix = match
        consumer = name[: -len(suffix)]
        if family == "targets":
            if not _NAME_RE.match(consumer):
                raise ValueError(
                    f"skin template {name!r} has an unsafe config dir {consumer!r}"
                )
        elif not _CONSUMER_RE.match(consumer):
            raise ValueError(
                f"skin template {name!r} has an unsafe artifact consumer {consumer!r}"
            )
        if consumer in consumers:
            raise ValueError(
                f"skin templates collide on consumer {consumer!r}: "
                f"{consumers[consumer]!r} and {name!r}"
            )
        output = f"{consumer}.{format}"
        if output in outputs:
            raise ValueError(f"skin templates collide on output {output!r}")
        consumers[consumer] = name
        outputs.add(output)
        templates.append(
            TemplateSpec(
                path=path,
                family=family,
                format=format,
                consumer=consumer,
                output=output,
            )
        )
    if not templates:
        raise ValueError(
            f"skin templates dir has no '*{_TEMPLATE_SUFFIX}' files "
            f"and no known artifact templates: {templates_dir}"
        )
    return tuple(templates)


def _substitute(
    text: str,
    values: dict[str, str],
    template: Path | str,
    *,
    layout_placeholders: frozenset[str] = frozenset(),
    reject_conditionals: bool = False,
    format: TemplateFormat | None = None,
) -> str:
    if reject_conditionals and _CONDITIONAL_RE.search(text):
        raise ValueError(
            f"skin template {template} has conditional syntax; v2 templates are unconditional"
        )

    def _replace(match: re.Match[str]) -> str:
        placeholder = match.group(1).strip()
        if placeholder in layout_placeholders:
            raise ValueError(
                f"skin template {template} uses layout placeholder "
                f"{{{{{placeholder}}}}}; layout.<app> is TSX-owned and never CSS"
            )
        if placeholder not in values:
            raise ValueError(
                f"skin template {template} references unknown placeholder "
                f"{{{{{placeholder}}}}}"
            )
        return values[placeholder]

    rendered = _PLACEHOLDER_RE.sub(_replace, text)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError(f"skin template {template} has an unresolved placeholder")
    if _PAINT_UNREPRESENTABLE in rendered:
        raise ValueError(
            f"skin template {template} uses a paint token that cannot be "
            f"projected into {format}"
        )
    return rendered


class SkinExpander(ISkinExpander):
    """Deterministic v1/v2 manifest expansion into a cache work dir."""

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
        manifest = load_skin_manifest(manifest_path)
        version = manifest_schema_version(manifest, manifest_path)
        if version == MANIFEST_V1:
            groups = _validate_v1_manifest(manifest, name, manifest_path)
            token_doc: dict[str, Any] = {"version": MANIFEST_V1, **groups}
            values = _placeholder_values(groups)
            layout_placeholders: frozenset[str] = frozenset()
            reject_conditionals = False
        else:
            validate_v2_manifest(manifest, name, manifest_path)
            # The skin cache is palette/mode-independent. Appearance generation
            # re-renders the templates with the selected mode before composing.
            token_doc = resolve_v2_token_document(manifest, None)
            values, layout_placeholders = _v2_placeholder_values(token_doc)
            reject_conditionals = True

        templates = discover_templates(templates_dir)

        templates_hash = _canonical_templates_hash(templates_dir)
        manifest_hash = hash_file(manifest_path)
        expected = skin_entry_hash(templates_hash, manifest_hash)
        if output_dir.name != expected:
            raise ValueError(
                f"skin output dir must be named {expected}, got {output_dir.name!r}"
            )

        rendered: dict[str, str] = {}
        for template in templates:
            try:
                source = template.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError(
                    f"cannot read skin template {template.path}: {exc}"
                ) from exc
            template_values = values
            if version == MANIFEST_V2:
                template_values, _layout = _v2_placeholder_values(
                    token_doc, template.format
                )
            rendered[template.output] = _substitute(
                source,
                template_values,
                template.path,
                layout_placeholders=layout_placeholders,
                reject_conditionals=reject_conditionals,
                format=template.format,
            )

        token_map = json.dumps(token_doc, indent=2, ensure_ascii=False) + "\n"
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
