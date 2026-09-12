"""AD-44 conformance: the embedded history schema equals the canonical one.

The canonical schema lives at `contracts/schemas/history.schema.json`; the
runtime package embeds a copy for enforcement. This executable check fails if
they ever drift — no prose, no reading a document on each side.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def test_canonical_history_schema_exists(repo_root: Path) -> None:
    assert (repo_root / "contracts" / "schemas" / "history.schema.json").is_file()


def test_embedded_history_schema_is_byte_identical_to_canonical(repo_root: Path) -> None:
    canonical = (repo_root / "contracts" / "schemas" / "history.schema.json").read_bytes()
    embedded = files("runtime.adapters").joinpath("schemas", "history.schema.json").read_bytes()
    assert embedded == canonical, (
        "embedded runtime/adapters/schemas/history.schema.json drifted from "
        "contracts/schemas/history.schema.json — re-copy the canonical file"
    )
