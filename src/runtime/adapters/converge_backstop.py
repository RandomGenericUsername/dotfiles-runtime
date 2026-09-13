"""Persisted last-converged input-hash backstop (AD-36/AD-44).

The backstop lives **under ``state_root``** — an output location, never a
watched root — so persisting it can never re-trigger the watcher. It is a
small JSON record ``{"version": 1, "input_hash": "<hex|sentinel>",
"converged_at": "<iso8601-z>"}`` (the timestamp is additive/non-breaking;
a pre-timestamp record still reads). Its shape is machine-defined by
``contracts/schemas/last-converged.schema.json`` (embedded byte-identical under
``schemas/``) and enforced here on read via ``fastjsonschema`` (AD-44).

Absent or corrupt backstop ⇒ ``None`` ("treat as changed"): the daemon then
converges and rewrites it. A record that violates the schema — corrupt JSON,
wrong version, missing ``input_hash``, or a mistyped field — is likewise read
as ``None`` and logged, never fatal. A symlinked record is refused (no-follow,
mirrors the desired/history/meta symlink policy) and read as ``None``. Writes
are atomic (tmp + ``os.replace``) so a crash mid-write never yields a torn
record — a torn record would merely read as corrupt and re-converge.

The read-only status surface (P5-1-4) consumes :meth:`read_record` to show
the inputs hash **and** the last-converged timestamp without requiring the
daemon. ``read()`` stays the hash-only contract the reactive use case binds.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import fastjsonschema

logger = logging.getLogger(__name__)

BACKSTOP_FILENAME = "last-converged.json"
BACKSTOP_VERSION = 1
BACKSTOP_SCHEMA_FILENAME = "last-converged.schema.json"

Validator = Callable[[object], None]


def _load_backstop_schema() -> dict[str, Any]:
    """Read the embedded last-converged record schema (draft-07)."""
    resource = files("runtime.adapters").joinpath("schemas", BACKSTOP_SCHEMA_FILENAME)
    return cast("dict[str, Any]", json.loads(resource.read_text(encoding="utf-8")))


@cache
def last_converged_validator() -> Validator:
    """Validator for a persisted last-converged record (draft-07, AD-44).

    Lazy and cached like the shared-contract validators: a corrupt embedded
    schema fails loud here (``RuntimeError``), never at import. The reader
    catches schema *value* violations and treats the record as changed.
    """
    try:
        schema = _load_backstop_schema()
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"corrupt contract schema {BACKSTOP_SCHEMA_FILENAME}: {exc}"
        ) from exc
    return cast("Validator", fastjsonschema.compile(schema))


@dataclass(frozen=True, slots=True)
class BackstopRecord:
    """One persisted last-converged record (inputs hash + timestamp)."""

    input_hash: str
    converged_at: str | None


def _now_iso_z() -> str:
    """Current UTC time as strict ISO-8601 ending with ``Z``."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class LastConvergedBackstop:
    """Read/write the persisted last-converged input hash under ``state_root``."""

    def __init__(self, state_root: Path) -> None:
        self._path = state_root / BACKSTOP_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> str | None:
        """Return the persisted input hash, or ``None`` when absent/corrupt."""
        record = self.read_record()
        return None if record is None else record.input_hash

    def read_record(self) -> BackstopRecord | None:
        """Return the full persisted record (hash + timestamp), or ``None``.

        Same absence/corruption policy as :meth:`read`; the timestamp is
        optional so records written before the field existed still read.
        """
        if self._path.is_symlink():
            logger.warning("backstop is a symlink (refusing to follow): %s", self._path)
            return None
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                logger.warning("backstop is a symlink (refusing to follow): %s", self._path)
                return None
            logger.warning("backstop unreadable; treating as changed: %s: %s", self._path, exc)
            return None
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("backstop unreadable; treating as changed: %s: %s", self._path, exc)
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("backstop corrupt; treating as changed: %s", self._path)
            return None
        try:
            last_converged_validator()(data)
        except fastjsonschema.JsonSchemaValueException:
            logger.warning(
                "backstop shape invalid (contract schema); treating as changed: %s", self._path
            )
            return None
        record = cast("dict[str, Any]", data)
        return BackstopRecord(
            input_hash=record["input_hash"],
            converged_at=record.get("converged_at"),
        )

    def write(self, input_hash: str) -> None:
        """Atomically persist ``input_hash`` + timestamp (tmp + fsync + replace)."""
        if not isinstance(input_hash, str) or not input_hash:
            raise ValueError(f"input_hash must be a non-empty string, got {input_hash!r}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "version": BACKSTOP_VERSION,
            "input_hash": input_hash,
            "converged_at": _now_iso_z(),
        }
        line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        tmp = self._path.with_name(f".{self._path.name}.tmp-{os.getpid()}")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                pass
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
