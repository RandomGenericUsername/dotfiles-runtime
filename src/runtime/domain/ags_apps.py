"""Pure AGS standalone-app contract types (AD-26/AD-29/AD-30).

This module carries the identity, ledger and result shapes the runtime uses
for a manifest-driven palette refresh. It is deliberately I/O-free: discovery,
subprocess invocation and atomic persistence live in ``runtime.adapters``.

The runtime is the sole emitter of the per-app result record
(``ags-app-refresh-result.schema.json``); app-internal logs are separate. A
``ReloadFailure`` is the runtime's structured failure entry. It is also a
``str`` subclass so the notification card and every pre-existing
``", ".join(reload_failures)`` consumer keep a string-compatible view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AppMode = Literal["live", "restart"]
AppStatus = Literal["applied", "deferred", "failed", "skipped"]

#: Fixed live-refresh request command (AD-27). One argument: the appearance hash.
PALETTE_REFRESH_COMMAND = "palette-refresh"


class ReloadFailure(str):
    """Structured consumer/app failure with a string notification view.

    Subclassing ``str`` is intentional: existing callers compare failures to a
    reloader class name or join them for the notification card, while callers
    that need structure read ``app``/``instance``/``status``/``reason``.
    """

    app: str
    instance: str
    mode: str
    status: str
    reason: str
    detail: str

    def __new__(
        cls,
        app: str,
        instance: str = "",
        mode: str = "",
        status: str = "failed",
        reason: str = "",
        detail: str = "",
    ) -> ReloadFailure:
        if instance:
            label = f"{app} ({instance}, {mode or 'unknown'})"
        else:
            label = app
        if status and status != "failed":
            label = f"{label}: {status}"
        if reason:
            label = f"{label}: {reason}"
        obj = super().__new__(cls, label)
        obj.app = app
        obj.instance = instance
        obj.mode = mode
        obj.status = status
        obj.reason = reason
        obj.detail = detail
        return obj

    def to_dict(self) -> dict[str, str]:
        """The structured ``{app, instance, mode, status, reason, detail}`` view."""
        return {
            "app": self.app,
            "instance": self.instance,
            "mode": self.mode,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
        }

    #: Alias for callers that name the structured view explicitly.
    as_dict = to_dict


@dataclass(frozen=True, slots=True)
class DeployedAppIdentity:
    """One validated deployed AGS app discovered from the installed spine."""

    app: str
    instance: str
    config_dir: str
    mode: AppMode
    holds_unsaved_state: bool
    link_path: str
    discovery_index: int


@dataclass(frozen=True, slots=True)
class AppDiscoveryFailure:
    """A deployed link whose manifest/identity could not be validated."""

    link: str
    reason: str
    discovery_index: int
    app: str = ""
    instance: str = ""
    config_dir: str = ""


@dataclass(frozen=True, slots=True)
class DiscoveredApp:
    """A validated app plus its live ownership state."""

    identity: DeployedAppIdentity
    running: bool
    pids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class AppDiscoveryResult:
    """Validated apps and structured failures from one discovery pass."""

    apps: tuple[DiscoveredApp, ...] = ()
    failures: tuple[AppDiscoveryFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class AppAppearanceState:
    """One app's last recorded appearance result (the ledger entry)."""

    app: str
    instance: str
    mode: AppMode
    status: AppStatus
    appearance_hash: str
    reason: str = ""
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class AppliedAppearance:
    """The one persisted applied-appearance ledger document (AD-29)."""

    version: int
    appearance_hash: str
    operation_id: str
    completed_at: str
    apps: dict[str, AppAppearanceState] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AppRefreshRecord:
    """One runtime-owned JSON Lines result record (AD-28)."""

    run_id: str
    app: str
    instance: str
    config_dir: str
    mode: AppMode
    appearance_hash: str
    status: AppStatus
    started_at: str
    ended_at: str
    duration_ms: int
    discovery_index: int
    phase: str = "refresh"
    reason: str = ""
    retryable: bool | None = None
    argv: tuple[str, ...] = ()
    exit_code: int | None = None
    response: dict[str, object] | None = None
    error: str | None = None
    timeout: bool = False

    def to_dict(self) -> dict[str, object]:
        """Contract-shaped JSON object (optional fields only when meaningful)."""
        record: dict[str, object] = {
            "runId": self.run_id,
            "app": self.app,
            "instance": self.instance,
            "configDir": self.config_dir,
            "mode": self.mode,
            "appearanceHash": self.appearance_hash,
            "status": self.status,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "durationMs": self.duration_ms,
            "discoveryIndex": self.discovery_index,
        }
        if self.phase:
            record["phase"] = self.phase
        if self.reason:
            record["reason"] = self.reason
        if self.retryable is not None:
            record["retryable"] = self.retryable
        if self.argv:
            record["argv"] = list(self.argv)
        if self.exit_code is not None:
            record["exitCode"] = self.exit_code
        if self.response is not None:
            record["response"] = self.response
        if self.error is not None:
            record["error"] = self.error
        if self.timeout:
            record["timeout"] = True
        return record


@dataclass(frozen=True, slots=True)
class AppRefreshOutcome:
    """Result of one appearance refresh across both lanes."""

    appearance_hash: str
    run_id: str
    records: tuple[AppRefreshRecord, ...] = ()
    failures: tuple[ReloadFailure, ...] = ()
    no_op: bool = False
