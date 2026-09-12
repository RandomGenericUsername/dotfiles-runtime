"""Embedded shared-contract schemas (AD-44).

The canonical schemas live at `contracts/schemas/` in the repo; the copies under
`runtime/adapters/schemas/` are the runtime's embedded definitions, kept
byte-identical by a repo conformance test. They live in `adapters/` because
loading a resource is I/O (the hexagon's outer layer).

Validators are LAZY (compiled on first use, cached): a corrupt schema must fail
loud at the call site (`RuntimeError`), never break every import of the package.
Each compiled validator carries a name for error messages. The kind-dispatched
meta validators exist so a same-kind shape failure names the field instead of a
generic `oneOf` miss.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import cache
from importlib.resources import files
from typing import Any, cast

import fastjsonschema

Validator = Callable[[object], None]

_KIND_TO_DEFINITION = {
    "wallpaper": "#/definitions/wallpaper",
    "palette": "#/definitions/palette",
    "effects": "#/definitions/effects",
    "icons": "#/definitions/icons",
}


def _load(filename: str) -> dict[str, Any]:
    resource = files("runtime.adapters").joinpath("schemas", filename)
    return cast("dict[str, Any]", json.loads(resource.read_text(encoding="utf-8")))


def _compile(schema: dict[str, Any]) -> Validator:
    return cast("Validator", fastjsonschema.compile(schema))


@cache
def _validator(filename: str) -> Validator:
    try:
        schema = _load(filename)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"corrupt contract schema {filename}: {exc}") from exc
    return _compile(schema)


def history_validator() -> Validator:
    """Validator for a history.jsonl line (draft-07)."""
    return _validator("history.schema.json")


def current_validator() -> Validator:
    """Validator for a v2 current.json dict (draft-07, post-migration)."""
    return _validator("current.schema.json")


def meta_validator() -> Validator:
    """Validator for any cache meta.json (the generic oneOf gate)."""
    return _validator("meta.schema.json")


@cache
def meta_validator_for_kind(kind: str) -> Validator:
    """Validator restricted to one meta kind, so errors name the field."""
    import copy

    schema = copy.deepcopy(_load("meta.schema.json"))
    schema.pop("oneOf", None)
    schema["$ref"] = _KIND_TO_DEFINITION[kind]
    return _compile(schema)


def load_history_schema() -> dict[str, Any]:
    """Embedded history.jsonl line schema (draft-07)."""
    return _load("history.schema.json")


def load_current_schema() -> dict[str, Any]:
    """Embedded current.json (v2) schema (draft-07)."""
    return _load("current.schema.json")


def load_meta_schema() -> dict[str, Any]:
    """Embedded cache meta.json schema (draft-07)."""
    return _load("meta.schema.json")
