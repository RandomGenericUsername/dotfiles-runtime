"""Applied-appearance ledger and the runtime operation lock (AD-29).

The runtime owns exactly one persisted ledger under the state root. It records
the appearance hash, operation identity, completion time and one per-app
result (``applied|deferred|failed|skipped``) so the next trigger can retry an
app whose last result was not applied. Writes are atomic (temp file + replace
+ fsync), and one flock serializes daemon/CLI/set operations for this ledger.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from runtime.domain.ags_apps import (
    AppAppearanceState,
    AppliedAppearance,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AppliedAppearanceLedger",
    "AppearanceLedger",
    "LEDGER_FILENAME",
    "OPERATION_LOCK_FILENAME",
    "appearance_hash_for_state",
    "ledger_path",
    "operation_lock_path",
]

LEDGER_FILENAME = "appearance-ledger.json"
OPERATION_LOCK_FILENAME = ".appearance-operation.lock"
LEDGER_VERSION = 1

_VALID_STATUSES: frozenset[str] = frozenset({"applied", "deferred", "failed", "skipped"})
_RETRY_STATUSES: frozenset[str] = frozenset({"deferred", "failed", "skipped"})


def ledger_path(state_root: Path) -> Path:
    """Path of the one applied-appearance ledger."""
    return state_root / LEDGER_FILENAME


def operation_lock_path(state_root: Path) -> Path:
    """Path of the runtime appearance-operation lock."""
    return state_root / OPERATION_LOCK_FILENAME


@contextmanager
def hold_operation_lock(state_root: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Hold the single appearance-operation lock (bounded flock).

    Raises:
        TimeoutError: another operation held the lock for the whole timeout.
        OSError: the lock file could not be opened/created.
    """
    path = operation_lock_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for the appearance operation lock {path}"
                    ) from None
                time.sleep(0.05)
                continue
            acquired = True
            break
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def appearance_hash_for_state(state: object) -> str:
    """Compute the appearance hash from a ``DesktopState`` (palette/icons/effects).

    Imports are local so domain models stay the only direct dependency of the
    caller; the hash formula lives in the adapter hashing module.
    """
    from runtime.adapters.hashing import appearance_hash

    palette = getattr(state, "palette", None)
    effects = getattr(state, "effects", None)
    icons = getattr(state, "icons", None)
    return appearance_hash(
        palette_hash=getattr(palette, "entry_hash", None),
        effects_hash=getattr(effects, "entry_hash", None),
        icons_hash=getattr(icons, "entry_hash", None),
        icon_templates_hash=getattr(icons, "input_templates_hash", None),
        icon_mappings_hash=getattr(icons, "input_mappings_hash", None),
    )


class AppearanceLedger:
    """Read/write the one applied-appearance ledger document."""

    def __init__(self, state_root: Path) -> None:
        self._path = ledger_path(state_root)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> AppliedAppearance | None:
        """Return the ledger, or ``None`` when absent/corrupt (logged)."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("appearance ledger: cannot read %s: %s", self._path, exc)
            return None
        try:
            data = json.loads(raw)
        except ValueError as exc:
            logger.warning("appearance ledger: malformed %s: %s", self._path, exc)
            return None
        if not isinstance(data, dict):
            logger.warning("appearance ledger: %s is not an object", self._path)
            return None
        version = data.get("version")
        appearance = data.get("appearanceHash")
        operation_id = data.get("operationId", "")
        completed_at = data.get("completedAt", "")
        apps_raw = data.get("apps")
        if (
            version != LEDGER_VERSION
            or not isinstance(appearance, str)
            or not isinstance(operation_id, str)
            or not isinstance(completed_at, str)
            or not isinstance(apps_raw, dict)
        ):
            logger.warning("appearance ledger: %s has an unsupported shape", self._path)
            return None

        apps: dict[str, AppAppearanceState] = {}
        for key, record in apps_raw.items():
            decoded = self._decode_app(key, record)
            if decoded is not None:
                apps[key] = decoded
        return AppliedAppearance(
            version=version,
            appearance_hash=appearance,
            operation_id=operation_id,
            completed_at=completed_at,
            apps=apps,
        )

    @staticmethod
    def _decode_app(key: object, record: object) -> AppAppearanceState | None:
        if not isinstance(key, str) or not isinstance(record, dict):
            return None
        app = record.get("app")
        instance = record.get("instance")
        mode = record.get("mode")
        status = record.get("status")
        appearance = record.get("appearanceHash")
        reason = record.get("reason", "")
        retryable = record.get("retryable", False)
        if (
            not isinstance(app, str)
            or not isinstance(instance, str)
            or mode not in ("live", "restart")
            or status not in _VALID_STATUSES
            or not isinstance(appearance, str)
            or not isinstance(reason, str)
            or not isinstance(retryable, bool)
        ):
            return None
        return AppAppearanceState(
            app=app,
            instance=instance,
            mode=mode,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            appearance_hash=appearance,
            reason=reason,
            retryable=retryable,
        )

    def save(self, ledger: AppliedAppearance) -> None:
        """Atomically replace the ledger (temp + fsync + ``os.replace``)."""
        payload = {
            "version": LEDGER_VERSION,
            "appearanceHash": ledger.appearance_hash,
            "operationId": ledger.operation_id,
            "completedAt": ledger.completed_at,
            "apps": {
                key: {
                    "app": value.app,
                    "instance": value.instance,
                    "mode": value.mode,
                    "status": value.status,
                    "appearanceHash": value.appearance_hash,
                    "reason": value.reason,
                    "retryable": value.retryable,
                }
                for key, value in sorted(ledger.apps.items())
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self._path.parent),
            prefix=".appearance-ledger-",
            suffix=".json.tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self._path)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    def needs_refresh(self, instance: str, appearance_hash: str) -> bool:
        """True when this app was not successfully applied at this appearance."""
        current = self.load()
        if current is None:
            return True
        recorded = current.apps.get(instance)
        if recorded is None:
            return True
        if recorded.appearance_hash != appearance_hash:
            return True
        if recorded.status in _RETRY_STATUSES:
            return True
        return recorded.status != "applied"

    def current_apps_for(self, appearance_hash: str) -> dict[str, AppAppearanceState]:
        """Return the per-app results recorded for exactly this appearance."""
        current = self.load()
        if current is None or current.appearance_hash != appearance_hash:
            return {}
        return dict(current.apps)


#: Explicit alias for the AD-29 name.
AppliedAppearanceLedger = AppearanceLedger
