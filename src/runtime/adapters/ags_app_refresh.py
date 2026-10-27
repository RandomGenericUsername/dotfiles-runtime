"""Manifest-driven AGS palette refresh: ledger gate, live lane, restart lane.

Integrates the contract in ``contracts/ags-app-refresh.md`` with the existing
serialized restart procedure (BUG-006):

- one applied-appearance ledger gate: an app is invoked only when its last
  successful appearance differs or its last result was failed/deferred/skipped;
- live apps refresh in parallel through ``ags request palette-refresh
  <appearance-hash> -i <instance>`` with a 5 s timeout and one JSON string ack;
- restart apps (the bar) run through the serialized ``AgsReloader`` procedure,
  including when their process is down, so a failed bar recovers;
- a failed live refresh never becomes a restart (AD-30);
- there is no immediate retry inside the operation;
- the runtime emits one JSON Lines record per app and writes the ledger
  atomically after both lanes settle (AD-28/AD-29).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import fastjsonschema

from runtime.adapters.ags_app_discovery import AppDiscovery
from runtime.adapters.ags_reloader import (
    AgsAppProcess,
    AgsReloader,
    _AgsQueryError,
    _list_ags_instances,
    _resolve_ags,
)
from runtime.adapters.appearance_ledger import (
    AppearanceLedger,
    appearance_hash_for_state,
    hold_operation_lock,
)
from runtime.adapters.contract_schemas import ags_app_refresh_result_validator
from runtime.domain.ags_apps import (
    AppAppearanceState,
    AppliedAppearance,
    AppRefreshOutcome,
    AppRefreshRecord,
    DeployedAppIdentity,
    DiscoveredApp,
    ReloadFailure,
)
from runtime.domain.models import DesktopState
from runtime.ports.ags_app_refresher import IAgsAppRefresher
from runtime.ports.state_repository import IStateRepository

logger = logging.getLogger(__name__)

__all__ = ["AgsAppRefresh", "AgsAppRefreshCoordinator", "LIVE_REFRESH_TIMEOUT"]

#: Per-app wall-clock timeout for the live lane (AD-27/AD-28).
LIVE_REFRESH_TIMEOUT = 5.0

_LIVE_PHASE = "palette-refresh"


@dataclass(frozen=True, slots=True)
class _LiveReply:
    status: str
    reason: str = ""
    retryable: bool | None = None
    response: dict[str, object] | None = None
    error: str | None = None
    exit_code: int | None = None
    timeout: bool = False


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _parse_live_reply(completed: object) -> _LiveReply:
    """Validate the one-string ack from one live app (contract §Transport)."""
    returncode = getattr(completed, "returncode", None)
    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    if not isinstance(returncode, int):
        return _LiveReply(
            status="failed",
            reason="missing return code",
            retryable=True,
            error="no return code",
        )
    if returncode != 0:
        detail = stderr.strip() or "no stderr"
        return _LiveReply(
            status="failed",
            reason=f"ags request exited with code {returncode}",
            retryable=True,
            exit_code=returncode,
            error=detail,
        )

    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return _LiveReply(
            status="failed",
            reason="empty response",
            retryable=True,
            exit_code=returncode,
            error="expected one JSON string on stdout",
        )
    if len(lines) != 1:
        return _LiveReply(
            status="failed",
            reason=f"expected exactly one JSON line, got {len(lines)}",
            retryable=True,
            exit_code=returncode,
            error="extra stdout output",
        )
    try:
        decoded = json.loads(lines[0])
    except ValueError as exc:
        return _LiveReply(
            status="failed",
            reason="response is not a JSON-encoded string",
            retryable=True,
            exit_code=returncode,
            error=str(exc),
        )
    if not isinstance(decoded, dict):
        return _LiveReply(
            status="failed",
            reason="response is not a JSON object string",
            retryable=True,
            exit_code=returncode,
            error=f"decoded {type(decoded).__name__}",
        )
    return _decode_response(decoded, exit_code=returncode)


def _decode_response(decoded: dict[str, object], *, exit_code: int) -> _LiveReply:
    keys = set(decoded)
    ok = decoded.get("ok")
    if not isinstance(ok, bool):
        return _LiveReply(
            status="failed",
            reason="response has no boolean ok",
            retryable=True,
            exit_code=exit_code,
            response=decoded,
            error="invalid ok field",
        )
    if ok:
        if keys - {"ok", "deferred"}:
            return _LiveReply(
                status="failed",
                reason="applied response has unknown keys",
                retryable=True,
                exit_code=exit_code,
                response=decoded,
                error=f"unexpected keys: {sorted(keys - {'ok', 'deferred'})}",
            )
        if "deferred" in decoded:
            deferred = decoded["deferred"]
            if not isinstance(deferred, str) or not deferred.strip():
                return _LiveReply(
                    status="failed",
                    reason="deferred reason is empty",
                    retryable=True,
                    exit_code=exit_code,
                    response=decoded,
                    error="deferred must be a non-empty string",
                )
            return _LiveReply(
                status="deferred",
                reason=deferred,
                exit_code=exit_code,
                response=decoded,
            )
        return _LiveReply(status="applied", exit_code=exit_code, response=decoded)

    if keys - {"ok", "error", "retryable"}:
        return _LiveReply(
            status="failed",
            reason="failed response has unknown keys",
            retryable=True,
            exit_code=exit_code,
            response=decoded,
            error=f"unexpected keys: {sorted(keys - {'ok', 'error', 'retryable'})}",
        )
    error = decoded.get("error")
    retryable = decoded.get("retryable")
    if not isinstance(error, str) or not error.strip() or not isinstance(retryable, bool):
        return _LiveReply(
            status="failed",
            reason="failed response is malformed",
            retryable=True,
            exit_code=exit_code,
            response=decoded,
            error="error must be non-empty and retryable boolean",
        )
    return _LiveReply(
        status="failed",
        reason=error,
        retryable=retryable,
        exit_code=exit_code,
        response=decoded,
    )


class AgsAppRefreshCoordinator(IAgsAppRefresher):
    """Run the ledger-gated live and restart lanes for one appearance."""

    def __init__(
        self,
        *,
        state_root: Path,
        install_spine: Path,
        state_repo: IStateRepository,
        ags_path: Path | None = None,
        ledger: AppearanceLedger | None = None,
        discovery: AppDiscovery | None = None,
        process_lister: Callable[[], Sequence[AgsAppProcess]] | None = None,
        instance_lister: Callable[[], Sequence[str]] | None = None,
        live_runner: Callable[..., object] = subprocess.run,
        restart_reloader: AgsReloader | None = None,
        launch_lock_path: Path | None = None,
        run_id_factory: Callable[[], str] = _run_id,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = LIVE_REFRESH_TIMEOUT,
    ) -> None:
        self._state_root = state_root
        self._install_spine = install_spine
        self._state_repo = state_repo
        self._ags_path = _resolve_ags(ags_path)
        self._ledger = ledger or AppearanceLedger(state_root)
        self._process_lister = process_lister
        self._discovery = discovery or AppDiscovery(install_spine, process_lister=process_lister)
        self._instance_lister = instance_lister
        self._live_runner = live_runner
        self._run_id_factory = run_id_factory
        self._clock = clock
        self._timeout = timeout
        self._restart_reloader = restart_reloader or AgsReloader(
            ags_path=self._ags_path,
            app_lister=process_lister,
            instance_lister=instance_lister,
            launch_lock_path=launch_lock_path,
        )

    @property
    def ledger(self) -> AppearanceLedger:
        return self._ledger

    def _live_names(self) -> tuple[list[str], str | None]:
        if self._instance_lister is not None:
            try:
                return list(self._instance_lister()), None
            except _AgsQueryError as exc:
                return [], str(exc)
            except Exception as exc:  # adapter boundary: structured, never crash
                return [], f"instance lister failed: {exc}"
        if self._ags_path is None:
            return [], "ags not found in PATH"
        try:
            return _list_ags_instances(self._ags_path), None
        except _AgsQueryError as exc:
            return [], str(exc)

    def refresh(self, state: DesktopState, *, trigger: str = "reconcile") -> AppRefreshOutcome:
        """Gate on the ledger, run both lanes, persist results atomically."""
        appearance = appearance_hash_for_state(state)
        run_id = self._run_id_factory()
        try:
            with hold_operation_lock(self._state_root):
                return self._refresh_locked(state, appearance, run_id, trigger)
        except TimeoutError as exc:
            failure = ReloadFailure(
                "AgsAppRefresh",
                status="deferred",
                reason=str(exc),
            )
            logger.warning("ags app refresh serialized behind another operation: %s", exc)
            return AppRefreshOutcome(
                appearance_hash=appearance,
                run_id=run_id,
                failures=(failure,),
            )

    # ------------------------------------------------------------------

    def _refresh_locked(
        self,
        state: DesktopState,
        appearance: str,
        run_id: str,
        trigger: str,
    ) -> AppRefreshOutcome:
        live_names, names_error = self._live_names()
        discovered = self._discovery.discover(live_names)
        previous = self._ledger.load()

        results: list[AppRefreshRecord] = []
        failures: list[ReloadFailure] = []
        ledger_apps: dict[str, AppAppearanceState] = {}
        live_plan: list[DiscoveredApp] = []
        restart_plan: list[DiscoveredApp] = []
        now = _now_z()

        for failure in discovered.failures:
            failure_entry = ReloadFailure(
                failure.app or failure.config_dir or "unknown-app",
                instance=failure.instance or failure.config_dir,
                mode="",
                status="failed",
                reason=failure.reason,
                detail=failure.link,
            )
            failures.append(failure_entry)
            key = failure.instance or failure.config_dir or failure.link
            ledger_apps[key] = AppAppearanceState(
                app=failure.app or key,
                instance=failure.instance or key,
                mode="live",
                status="failed",
                appearance_hash=appearance,
                reason=failure.reason,
                retryable=True,
            )

        for app in discovered.apps:
            identity = app.identity
            prior = previous.apps.get(identity.instance) if previous is not None else None
            if (
                prior is not None
                and prior.appearance_hash == appearance
                and prior.status == "applied"
            ):
                results.append(
                    self._record(
                        run_id,
                        identity,
                        appearance,
                        status="skipped",
                        reason="ledger current for this appearance",
                        started=now,
                        ended=now,
                        duration_ms=0,
                    )
                )
                ledger_apps[identity.instance] = prior
                continue
            if identity.mode == "restart":
                restart_plan.append(app)
            elif not app.running:
                results.append(
                    self._record(
                        run_id,
                        identity,
                        appearance,
                        status="skipped",
                        reason=(
                            names_error if names_error is not None else "live app is not running"
                        ),
                        started=now,
                        ended=now,
                        duration_ms=0,
                    )
                )
                ledger_apps[identity.instance] = AppAppearanceState(
                    app=identity.app,
                    instance=identity.instance,
                    mode=identity.mode,
                    status="skipped",
                    appearance_hash=appearance,
                    reason=names_error or "live app is not running",
                )
            else:
                live_plan.append(app)

        live_results = self._run_live_lane(live_plan, appearance, run_id)
        for app, result in live_results:
            results.append(result)
            identity = app.identity
            ledger_apps[identity.instance] = AppAppearanceState(
                app=identity.app,
                instance=identity.instance,
                mode=identity.mode,
                status=result.status,  # type: ignore[arg-type]
                appearance_hash=appearance,
                reason=result.reason,
                retryable=bool(result.retryable),
            )
            if result.status in ("failed", "deferred"):
                failures.append(
                    ReloadFailure(
                        identity.app,
                        instance=identity.instance,
                        mode=identity.mode,
                        status=result.status,
                        reason=result.reason,
                        detail=result.error or "",
                    )
                )

        restart_results = self._run_restart_lane(restart_plan, appearance, run_id)
        for app, result in restart_results:
            results.append(result)
            identity = app.identity
            ledger_apps[identity.instance] = AppAppearanceState(
                app=identity.app,
                instance=identity.instance,
                mode=identity.mode,
                status=result.status,  # type: ignore[arg-type]
                appearance_hash=appearance,
                reason=result.reason,
                retryable=bool(result.retryable),
            )
            if result.status == "failed":
                failures.append(
                    ReloadFailure(
                        identity.app,
                        instance=identity.instance,
                        mode=identity.mode,
                        status=result.status,
                        reason=result.reason,
                        detail=result.error or "",
                    )
                )

        # If the palette pointer moved while the lanes ran, no result may be
        # recorded as applied for the old hash (AD-29). Downgrade to deferred so
        # the next trigger refreshes against the authoritative appearance.
        latest = self._state_repo.load_current()
        if latest is not None:
            latest_hash = appearance_hash_for_state(latest)
            if latest_hash != appearance:
                results = [
                    self._downgrade_to_deferred(record) if record.status == "applied" else record
                    for record in results
                ]
                ledger_apps = {
                    key: self._deferred_state(value) if value.status == "applied" else value
                    for key, value in ledger_apps.items()
                }
                failures.extend(
                    ReloadFailure(
                        record.app,
                        instance=record.instance,
                        mode=record.mode,
                        status="deferred",
                        reason="appearance changed during refresh",
                    )
                    for record in results
                    if record.status == "deferred"
                    and record.reason == "appearance changed during refresh"
                )

        results.sort(key=lambda record: record.discovery_index)
        failures.sort(key=lambda failure: (failure.app, failure.instance))

        # A ledger-current skip is a true no-op. A skip because a live app is
        # down must still be persisted so the next trigger retries it once it
        # is running (AD-29).
        no_op = not failures and all(
            record.status == "skipped"
            and record.reason == "ledger current for this appearance"
            for record in results
        )
        if no_op:
            return AppRefreshOutcome(
                appearance_hash=appearance,
                run_id=run_id,
                records=tuple(results),
                no_op=True,
            )

        if results:
            self._write_records(run_id, results)
        self._ledger.save(
            AppliedAppearance(
                version=1,
                appearance_hash=appearance,
                operation_id=run_id,
                completed_at=_now_z(),
                apps=ledger_apps,
            )
        )
        logger.info(
            "ags app refresh trigger=%s run=%s appearance=%s apps=%d failures=%d",
            trigger,
            run_id,
            appearance[:12],
            len(results),
            len(failures),
        )
        return AppRefreshOutcome(
            appearance_hash=appearance,
            run_id=run_id,
            records=tuple(results),
            failures=tuple(failures),
            no_op=no_op,
        )

    # ------------------------------------------------------------------

    def _run_live_lane(
        self,
        apps: Sequence[DiscoveredApp],
        appearance: str,
        run_id: str,
    ) -> list[tuple[DiscoveredApp, AppRefreshRecord]]:
        if not apps:
            return []
        ags = self._ags_path
        if ags is None:
            return [
                (
                    app,
                    self._record(
                        run_id,
                        app.identity,
                        appearance,
                        status="failed",
                        reason="ags not found in PATH",
                        retryable=True,
                        started=_now_z(),
                        ended=_now_z(),
                        duration_ms=0,
                        error="ags binary not found",
                    ),
                )
                for app in apps
            ]
        with ThreadPoolExecutor(max_workers=len(apps)) as pool:
            futures = [
                pool.submit(self._refresh_live_app, ags, app, appearance, run_id) for app in apps
            ]
            return [future.result() for future in futures]

    def _refresh_live_app(
        self,
        ags: Path,
        app: DiscoveredApp,
        appearance: str,
        run_id: str,
    ) -> tuple[DiscoveredApp, AppRefreshRecord]:
        identity = app.identity
        argv = [
            str(ags),
            "request",
            "palette-refresh",
            appearance,
            "-i",
            identity.instance,
        ]
        started_at = _now_z()
        started = self._clock()
        timeout = False
        try:
            completed = self._live_runner(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            timeout = True
            reply = _LiveReply(
                status="failed",
                reason=f"timed out after {self._timeout:g} s",
                retryable=True,
                error="timeout",
                timeout=True,
            )
        except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
            reply = _LiveReply(
                status="failed",
                reason=f"cannot invoke ags request: {exc}",
                retryable=True,
                error=str(exc),
            )
        else:
            reply = _parse_live_reply(completed)
        duration_ms = max(0, int((self._clock() - started) * 1000))
        return (
            app,
            self._record(
                run_id,
                identity,
                appearance,
                status=reply.status,
                reason=reply.reason,
                retryable=reply.retryable,
                started=started_at,
                ended=_now_z(),
                duration_ms=duration_ms,
                argv=tuple(argv),
                exit_code=reply.exit_code,
                response=reply.response,
                error=reply.error,
                timeout=timeout or reply.timeout,
            ),
        )

    def _run_restart_lane(
        self,
        apps: Sequence[DiscoveredApp],
        appearance: str,
        run_id: str,
    ) -> list[tuple[DiscoveredApp, AppRefreshRecord]]:
        results: list[tuple[DiscoveredApp, AppRefreshRecord]] = []
        for app in apps:
            identity = app.identity
            started_at = _now_z()
            started = self._clock()
            try:
                ok = self._restart_reloader.restart_deployed_app(
                    instance=identity.instance,
                    config_dir=Path(identity.link_path),
                    argv=[
                        str(self._ags_path or "ags"),
                        "run",
                        "-d",
                        identity.link_path,
                    ],
                    old_pids=app.pids,
                )
            except Exception as exc:  # adapter boundary: never crash the trigger
                logger.warning("ags restart lane failed for %s: %s", identity.instance, exc)
                ok = False
                error = str(exc)
            else:
                error = ""
            duration_ms = max(0, int((self._clock() - started) * 1000))
            if ok:
                results.append(
                    (
                        app,
                        self._record(
                            run_id,
                            identity,
                            appearance,
                            status="applied",
                            started=started_at,
                            ended=_now_z(),
                            duration_ms=duration_ms,
                            argv=(
                                str(self._ags_path or "ags"),
                                "run",
                                "-d",
                                identity.link_path,
                            ),
                            exit_code=0,
                        ),
                    )
                )
            else:
                reason = "restart did not confirm the intended instance"
                results.append(
                    (
                        app,
                        self._record(
                            run_id,
                            identity,
                            appearance,
                            status="failed",
                            reason=reason,
                            retryable=True,
                            started=started_at,
                            ended=_now_z(),
                            duration_ms=duration_ms,
                            argv=(
                                str(self._ags_path or "ags"),
                                "run",
                                "-d",
                                identity.link_path,
                            ),
                            error=error or reason,
                        ),
                    )
                )
        return results

    # ------------------------------------------------------------------

    @staticmethod
    def _record(
        run_id: str,
        identity: DeployedAppIdentity,
        appearance: str,
        *,
        status: str,
        started: str,
        ended: str,
        duration_ms: int,
        reason: str = "",
        retryable: bool | None = None,
        argv: tuple[str, ...] = (),
        exit_code: int | None = None,
        response: dict[str, object] | None = None,
        error: str | None = None,
        timeout: bool = False,
    ) -> AppRefreshRecord:
        return AppRefreshRecord(
            run_id=run_id,
            app=identity.app,
            instance=identity.instance,
            config_dir=identity.config_dir,
            mode=identity.mode,
            appearance_hash=appearance,
            status=status,  # type: ignore[arg-type]
            started_at=started,
            ended_at=ended,
            duration_ms=duration_ms,
            discovery_index=identity.discovery_index,
            phase=_LIVE_PHASE if identity.mode == "live" else "restart",
            reason=reason,
            retryable=retryable,
            argv=argv,
            exit_code=exit_code,
            response=response,
            error=error,
            timeout=timeout,
        )

    @staticmethod
    def _downgrade_to_deferred(record: AppRefreshRecord) -> AppRefreshRecord:
        return AppRefreshRecord(
            run_id=record.run_id,
            app=record.app,
            instance=record.instance,
            config_dir=record.config_dir,
            mode=record.mode,
            appearance_hash=record.appearance_hash,
            status="deferred",
            started_at=record.started_at,
            ended_at=record.ended_at,
            duration_ms=record.duration_ms,
            discovery_index=record.discovery_index,
            phase=record.phase,
            reason="appearance changed during refresh",
            retryable=True,
            argv=record.argv,
            exit_code=record.exit_code,
            response=record.response,
            error=record.error,
            timeout=record.timeout,
        )

    @staticmethod
    def _deferred_state(state: AppAppearanceState) -> AppAppearanceState:
        return AppAppearanceState(
            app=state.app,
            instance=state.instance,
            mode=state.mode,
            status="deferred",
            appearance_hash=state.appearance_hash,
            reason="appearance changed during refresh",
            retryable=True,
        )

    def _write_records(self, run_id: str, records: Sequence[AppRefreshRecord]) -> None:
        """Write runtime-owned per-app JSONL logs (validated against contract)."""
        root = self._state_root / "logs" / "ags-app-refresh" / run_id
        try:
            root.mkdir(parents=True, exist_ok=True)
            validate = ags_app_refresh_result_validator()
            for record in records:
                payload = record.to_dict()
                try:
                    validate(payload)
                except fastjsonschema.JsonSchemaValueException as exc:
                    logger.warning(
                        "ags app refresh: record for %s does not match the contract: %s",
                        record.instance,
                        exc.message,
                    )
                    continue
                path = root / f"{record.discovery_index:02d}-{record.instance}.jsonl"
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            logger.warning("ags app refresh: cannot write runtime logs: %s", exc)

    # ------------------------------------------------------------------
    # Introspection helpers for tests / inspection surfaces.
    # ------------------------------------------------------------------

    def last_ledger(self) -> AppliedAppearance | None:
        return self._ledger.load()

    def discovery(self, live_names: Sequence[str]) -> tuple[DiscoveredApp, ...]:
        return self._discovery.discover(live_names).apps


#: Short alias used by composition roots and tests.
AgsAppRefresh = AgsAppRefreshCoordinator
