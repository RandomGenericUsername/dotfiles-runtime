"""Skin manifest parser + deterministic template expander (FROZEN SKIN SPEC v1).

The adapter owns all filesystem reads/writes for the skin layer. The frozen
scope is a palette/ink mapping + paint + rhythm: no structure or variant
tokens, and no Jinja runtime — expansion is the literal
``{{SKIN_<TOKEN>}}`` substitution the spec pins.

Manifest v1 (JSON or TOML)::

    name = "glass"
    version = 1
    description = "optional"
    [tokens]
    surface_alpha = "0.82"
    radius = "12px"
    [apps]
    ags = "ags.css.tpl"
    gtk = "gtk.css.tpl"

``apps`` maps an app name to a template path relative to the shared templates
dir. Output is exactly ``<app>.css`` per app plus ``skin.json`` (the resolved
flat token map for ``skinValue`` consumers). Template context is the snake-case
token key uppercased: token ``surface_alpha`` → ``{{SKIN_SURFACE_ALPHA}}``.

Validation is fail-closed: unknown keys, unsafe paths, non-scalar tokens,
duplicate outputs and unresolved placeholders raise ``ValueError`` naming the
manifest/template and nothing is written outside the entry work dir.
"""

from __future__ import annotations

import json
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from runtime.adapters.hashing import hash_file, skin_entry_hash
from runtime.domain.models import SkinArtifacts, SkinEntry
from runtime.ports.skin_expander import ISkinExpander

_MANIFEST_KEYS = frozenset({"name", "version", "description", "tokens", "apps"})
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TOKEN_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PLACEHOLDER_RE = re.compile(r"\{\{SKIN_([A-Z0-9_]+)\}\}")

Scalar = str | int | float | bool


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _token_placeholder(key: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").upper()
    return f"SKIN_{normalized}"


def _scalar_text(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _safe_relpath(raw: str, *, field: str) -> str:
    path = Path(raw)
    if not raw or path.is_absolute() or ".." in path.parts or "\\" in raw:
        raise ValueError(f"manifest {field} must be a relative posix path, got {raw!r}")
    if not (raw.endswith(".css") or raw.endswith(".css.tpl")):
        raise ValueError(f"manifest {field} must name a .css/.css.tpl template, got {raw!r}")
    return path.as_posix()


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
    unknown = set(data) - _MANIFEST_KEYS
    if unknown:
        raise ValueError(f"skin manifest has unknown keys {sorted(unknown)}: {path}")
    return cast("dict[str, Any]", data)


def _validate_manifest(
    data: dict[str, Any], selected_name: str, path: Path
) -> tuple[dict[str, Scalar], dict[str, str]]:
    name = data.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"skin manifest 'name' must be a safe lowercase name: {path}")
    if name != selected_name:
        raise ValueError(
            f"skin manifest name {name!r} does not match selected skin {selected_name!r}: {path}"
        )
    version = data.get("version", "<absent>")
    if type(version) is not int or version != 1:
        raise ValueError(f"skin manifest version must be 1, got {version!r}: {path}")
    description = data.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError(f"skin manifest 'description' must be a string: {path}")

    tokens_raw = data.get("tokens", {})
    if not isinstance(tokens_raw, dict):
        raise ValueError(f"skin manifest 'tokens' must be a table/object: {path}")
    tokens: dict[str, Scalar] = {}
    placeholders: set[str] = set()
    for key, value in tokens_raw.items():
        if not isinstance(key, str) or not _TOKEN_KEY_RE.match(key):
            raise ValueError(f"skin token key {key!r} is not a safe token name: {path}")
        if isinstance(value, bool) or isinstance(value, (str, int, float)):
            tokens[key] = value
        else:
            raise ValueError(f"skin token {key!r} must be a string/number/bool: {path}")
        placeholder = _token_placeholder(key)
        if placeholder in placeholders:
            raise ValueError(f"skin tokens collide on placeholder {placeholder!r}: {path}")
        placeholders.add(placeholder)

    apps_raw = data.get("apps")
    if not isinstance(apps_raw, dict) or not apps_raw:
        raise ValueError(f"skin manifest 'apps' must be a non-empty table/object: {path}")
    apps: dict[str, str] = {}
    outputs: set[str] = set()
    for app, template in apps_raw.items():
        if not isinstance(app, str):
            raise ValueError(f"skin app key must be a string, got {app!r}: {path}")
        app_name = app[:-4] if app.endswith(".css") else app
        if not _NAME_RE.match(app_name):
            raise ValueError(f"skin app name {app!r} is not a safe lowercase name: {path}")
        if not isinstance(template, str):
            raise ValueError(f"skin app {app!r} template must be a string: {path}")
        output = f"{app_name}.css"
        if output in outputs:
            raise ValueError(f"skin manifest maps two apps to {output!r}: {path}")
        outputs.add(output)
        apps[app_name] = _safe_relpath(template, field=f"apps.{app}")
    return tokens, apps


def _resolve_template(templates_dir: Path, relpath: str) -> Path:
    candidate = templates_dir / relpath
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ValueError(f"cannot resolve skin template {candidate}: {exc}") from exc
    try:
        inside = resolved.is_relative_to(templates_dir.resolve())
    except OSError as exc:
        raise ValueError(f"cannot resolve skin templates dir {templates_dir}: {exc}") from exc
    if not inside:
        raise ValueError(f"skin template escapes the templates dir: {candidate}")
    if not resolved.is_file():
        raise ValueError(f"skin template not found: {candidate}")
    return resolved


def _substitute(text: str, string_tokens: dict[str, str], template: Path) -> str:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in string_tokens:
            raise ValueError(f"skin template {template} references undefined token {key!r}")
        return string_tokens[key]

    rendered = _PLACEHOLDER_RE.sub(_replace, text)
    if "{{SKIN_" in rendered:
        raise ValueError(f"skin template {template} has an unresolved {{SKIN_*}} placeholder")
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
        tokens, apps = _validate_manifest(manifest, name, manifest_path)
        string_tokens = {
            _token_placeholder(key).removeprefix("SKIN_"): _scalar_text(value)
            for key, value in tokens.items()
        }

        templates_hash = _canonical_templates_hash(templates_dir)
        manifest_hash = hash_file(manifest_path)
        expected = skin_entry_hash(templates_hash, manifest_hash)
        if output_dir.name != expected:
            raise ValueError(
                f"skin output dir must be named {expected}, got {output_dir.name!r}"
            )

        rendered: dict[str, str] = {}
        for app, template_rel in sorted(apps.items()):
            template = _resolve_template(templates_dir, template_rel)
            try:
                source = template.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError(f"cannot read skin template {template}: {exc}") from exc
            rendered[f"{app}.css"] = _substitute(source, string_tokens, template)

        token_map = json.dumps(tokens, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
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
