"""AD-44 conformance: the embedded history schema equals the canonical one.

The canonical schema lives at `contracts/schemas/history.schema.json`; the
runtime package embeds a copy for enforcement. This executable check fails if
they ever drift — no prose, no reading a document on each side.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def _canonical_history_schema() -> Path:
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "contracts" / "schemas" / "history.schema.json"


def test_canonical_history_schema_exists() -> None:
    assert _canonical_history_schema().is_file()


def test_embedded_history_schema_is_byte_identical_to_canonical() -> None:
    canonical = _canonical_history_schema().read_bytes()
    embedded = files("runtime.adapters").joinpath("schemas", "history.schema.json").read_bytes()
    assert embedded == canonical, (
        "embedded runtime/adapters/schemas/history.schema.json drifted from "
        "contracts/schemas/history.schema.json — re-copy the canonical file"
    )
