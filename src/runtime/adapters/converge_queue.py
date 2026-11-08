"""Daemon-owned converge job queue (v3 ``generations.md`` §3.1).

The queue is layered over the existing ``org.dotfiles.Events`` job surface:
``BeginJob("converge:<intentHash>", ttl)`` allocates a daemon job id (emitting
``JobStarted``), the worker runs the single ``converge(intent)`` function, and
``EndJob(job_id, exit)`` emits ``JobFinished``. A result file keyed by job id
carries the committed ``genHash`` and per-app status to the waiting CLI.

Queue policy:

- identical intent hashes coalesce into one execution (every waiter id is
  ended with the same result);
- the newest queued intent supersedes older queued (not running) intents;
  their waiter ids are merged into the newest job, never cancelled mid-run;
- a running job is never cancelled. A new intent of the same hash joins it;
  a different hash waits for it to finish.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.adapters.converge_types import ConvergeQueueResult
from runtime.adapters.dbus_converge_client import CONVERGE_JOB_PREFIX, RESULT_DIR
from runtime.ports.event_bus import IJobRegistry

logger = logging.getLogger(__name__)

__all__ = ["ConvergeJobQueue"]


@dataclass
class _Pending:
    intent_hash: str
    job_ids: list[str] = field(default_factory=list)

    @property
    def primary(self) -> str:
        return self.job_ids[0]


class ConvergeJobQueue:
    """Serialized, coalescing daemon queue with a result file per waiter."""

    def __init__(
        self,
        *,
        registry: IJobRegistry,
        state_root: Path,
        converge: Callable[[str, str], ConvergeQueueResult],
        registry_lock: threading.Lock,
        ttl: float = 300.0,
    ) -> None:
        self._registry = registry
        self._state_root = state_root
        self._converge = converge
        self._registry_lock = registry_lock
        self._ttl = ttl
        self._condition = threading.Condition()
        self._pending: _Pending | None = None
        self._running: _Pending | None = None
        self._stopped = False
        self._thread: threading.Thread | None = None

    @property
    def result_root(self) -> Path:
        return self._state_root / RESULT_DIR

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped = False
        self._thread = threading.Thread(target=self._serve, name="converge-queue", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            pending = self._pending
            self._pending = None
            self._condition.notify_all()
        if pending is not None:
            self._finish(
                pending,
                ConvergeQueueResult(
                    generation_hash=None,
                    error="daemon stopping before the queued converge ran",
                    exit_code=1,
                ),
            )
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        self._thread = None

    def submit(self, kind: str, ttl: float) -> str | None:
        """Intercept ``converge:<hash>`` BeginJob; return the waiter job id."""
        if not kind.startswith(CONVERGE_JOB_PREFIX):
            return None
        intent_hash = kind[len(CONVERGE_JOB_PREFIX) :]
        if len(intent_hash) != 64 or any(c not in "0123456789abcdef" for c in intent_hash):
            raise ValueError(
                "converge kind must carry a 64-char lowercase hex intent hash: "
                f"{kind!r}"
            )
        # The caller (HubService) holds the shared registry lock; the domain
        # validates and records JobStarted. The sink is queued and drained by
        # the dispatch call after this returns.
        job_id = self._registry.begin(kind, float(ttl or self._ttl))
        with self._condition:
            if self._running is not None and self._running.intent_hash == intent_hash:
                self._running.job_ids.append(job_id)
                return job_id
            if self._pending is not None and self._pending.intent_hash == intent_hash:
                self._pending.job_ids.append(job_id)
                return job_id
            if self._pending is not None:
                # Supersede every queued (not running) intent: its waiters join
                # the newest intent and receive the newest result.
                self._pending = _Pending(
                    intent_hash=intent_hash,
                    job_ids=[job_id, *self._pending.job_ids],
                )
            else:
                self._pending = _Pending(intent_hash=intent_hash, job_ids=[job_id])
            self._condition.notify_all()
        return job_id

    def _serve(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stopped:
                    self._condition.wait()
                if self._stopped:
                    return
                pending = self._pending
                self._pending = None
                self._running = pending
            try:
                result = self._converge(pending.intent_hash, pending.primary)
            except Exception as exc:  # never kill the queue thread
                logger.exception("converge queue: job %s failed unexpectedly", pending.primary)
                result = ConvergeQueueResult(
                    generation_hash=None,
                    error=f"{type(exc).__name__}: {exc}",
                    exit_code=1,
                )
            finally:
                with self._condition:
                    self._running = None
            self._finish(pending, result)

    def _finish(self, pending: _Pending, result: ConvergeQueueResult) -> None:
        payload: dict[str, Any] = {
            "jobId": pending.primary,
            "intentHash": pending.intent_hash,
            "generationHash": result.generation_hash,
            "degraded": result.degraded,
            "appResults": list(result.app_results),
            "regenerated": list(result.regenerated),
        }
        if result.error:
            payload["error"] = result.error
        if result.superseded_by:
            payload["supersededBy"] = result.superseded_by
        self.result_root.mkdir(parents=True, exist_ok=True)
        for job_id in pending.job_ids:
            self._write_result(job_id, {**payload, "jobId": job_id})
            with self._registry_lock:
                self._registry.end(job_id, result.exit_code)

    def _write_result(self, job_id: str, payload: dict[str, Any]) -> None:
        target = self.result_root / f"{job_id}.json"
        tmp = self.result_root / f".{job_id}.tmp.{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            tmp.write_text(
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with tmp.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(str(tmp), str(target))
        finally:
            tmp.unlink(missing_ok=True)
