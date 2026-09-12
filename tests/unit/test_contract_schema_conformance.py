"""AD-44 conformance: embedded schemas equal their canonical definitions.

The canonical schemas live at `contracts/schemas/`; the runtime package embeds
copies for enforcement. This executable check fails if they ever drift.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

_SCHEMAS = ("history", "current", "meta")


@pytest.mark.parametrize("name", _SCHEMAS)
def test_canonical_schema_exists(name: str, repo_root: Path) -> None:
    assert (repo_root / "contracts" / "schemas" / f"{name}.schema.json").is_file()


@pytest.mark.parametrize("name", _SCHEMAS)
def test_embedded_schema_is_byte_identical_to_canonical(name: str, repo_root: Path) -> None:
    canonical = (repo_root / "contracts" / "schemas" / f"{name}.schema.json").read_bytes()
    embedded = files("runtime.adapters").joinpath("schemas", f"{name}.schema.json").read_bytes()
    assert embedded == canonical, (
        f"embedded runtime/adapters/schemas/{name}.schema.json drifted from "
        f"contracts/schemas/{name}.schema.json — re-copy the canonical file"
    )


def test_corrupt_schema_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """A corrupt schema must fail loud at the validator, never break imports."""

    import runtime.adapters.contract_schemas as cs

    cs._validator.cache_clear()
    try:

        def _boom(filename: str) -> dict[str, object]:
            raise OSError("disk gone")

        monkeypatch.setattr(cs, "_load", _boom)
        with pytest.raises(RuntimeError, match="corrupt contract schema"):
            cs.history_validator()
    finally:
        cs._validator.cache_clear()
