"""Embedded shared-contract schemas (AD-44).

The canonical schema lives at `contracts/schemas/` in the repo; the copy under
`runtime/adapters/schemas/` is the runtime's embedded definition, kept
byte-identical by a repo conformance test. It lives in `adapters/` because
loading a resource is I/O (the hexagon's outer layer), and the compiled
validator is injected into the reader from there.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any, cast


@lru_cache(maxsize=1)
def load_history_schema() -> dict[str, Any]:
    """Load the embedded history.jsonl line schema (draft-07)."""
    resource = files("runtime.adapters").joinpath("schemas", "history.schema.json")
    return cast("dict[str, Any]", json.loads(resource.read_text(encoding="utf-8")))
