"""AD-44 conformance: embedded schemas equal their canonical definitions.

The canonical schemas live at `contracts/schemas/`; the runtime package embeds
copies for enforcement. This executable check fails if they ever drift.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import fastjsonschema
import pytest

_SCHEMAS = ("history", "current", "meta", "last-converged")


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


def test_backstop_reader_validator_is_the_embedded_schema() -> None:
    """The reader validates records against the embedded (canonical) schema."""
    from runtime.adapters.converge_backstop import last_converged_validator

    validate = last_converged_validator()
    validate({"version": 1, "input_hash": "ab" * 32})
    validate({"version": 1, "input_hash": "ab" * 32, "converged_at": "2026-09-13T00:00:00Z"})
    with pytest.raises(fastjsonschema.JsonSchemaValueException):
        validate({"version": 2, "input_hash": "ab" * 32})


def test_backstop_reader_treats_schema_violation_as_changed(tmp_path: Path) -> None:
    from runtime.adapters.converge_backstop import BackstopRecord, LastConvergedBackstop

    path = tmp_path / "last-converged.json"
    path.write_text('{"version": 1, "input_hash": "ab", "converged_at": 5}', encoding="utf-8")
    assert LastConvergedBackstop(tmp_path).read_record() is None

    path.write_text('{"version": 1, "input_hash": "ab"}', encoding="utf-8")
    assert LastConvergedBackstop(tmp_path).read_record() == BackstopRecord("ab", None)


def test_backstop_reader_corrupt_schema_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt embedded backstop schema fails loud at the validator."""

    import runtime.adapters.converge_backstop as cb

    cb.last_converged_validator.cache_clear()
    try:

        def _boom() -> dict[str, object]:
            raise OSError("disk gone")

        monkeypatch.setattr(cb, "_load_backstop_schema", _boom)
        with pytest.raises(RuntimeError, match="corrupt contract schema"):
            cb.last_converged_validator()
    finally:
        cb.last_converged_validator.cache_clear()
