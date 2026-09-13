"""Hub job-registry domain core — pure job lifecycle + leases + epoch (Phase 5).

P5-1-2a: transport-free. The registry owns job identity, lease deadlines,
transitive PID adoption (AD-37), the kind-keyed control allowlist (H1), and
the monotonic epoch. Every state change appends a :class:`HubEvent` to an
injectable sink — P5-1-2b binds sink records to the
``org.dotfiles.Events1`` signals (``JobStarted``/``JobProgress``/
``JobFinished``/``JobsCleared`` carry the same members; see
``contracts/event-contract.json``).

Purity (AD-1, AD-14): the clock, the id factory, and the sink are injected
— no I/O, no ``time``/``uuid`` imports (forbidden in domain by the
layering test), no D-Bus. Expiry is lazy (evaluated on every call against
the injected monotonic clock): no threads, no timers, no polling, no sweep
loop. The daemon holds no lock across hub calls — hub calls are registry
mutations, never use-case calls (AD-35).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from runtime.domain.models import (
    JobEnded,
    NotControllable,
    UnknownJob,
)

__all__ = [
    "CONTROL_ALLOWLIST",
    "SYNTHETIC_EXPIRY_EXIT_CODE",
    "EventHub",
    "HubEvent",
]

#: Exit code of the synthetic finish the hub records when a lease expires
#: (mirrors ``contracts/event-contract.json`` `job_lease.expiry_exit_code`).
SYNTHETIC_EXPIRY_EXIT_CODE: int = -1

#: The single control path (H1): allowed actions per job kind. Unknown kinds
#: map to the empty set (deny-all), so any ``Control`` on them raises
#: :class:`NotControllable`. Static on purpose (Gate-1 rec 5): adding a
#: ``BeginJob`` parameter or a ``RegisterActions`` method would grow the
#: wire surface, while a table change stays additive-compatible on
#: ``Events1``. ``BeginJob`` keeps its 2-arg contract signature for the
#: same reason.
CONTROL_ALLOWLIST: Mapping[str, frozenset[str]] = {
    "capture": frozenset({"pause", "resume", "stop"}),
}


@dataclass(frozen=True, slots=True)
class HubEvent:
    """One hub state change for the injectable sink (2b signal seam).

    Field set mirrors the contract signal members (``contracts/
    event-contract.json`` `signals`): ``JobStarted`` carries
    (job_id, kind, epoch), ``JobProgress`` (job_id, fraction, epoch),
    ``JobFinished`` (job_id, exit_code, epoch), ``JobsCleared`` (epoch) —
    event tags stay snake_case (Python convention); 2b maps them to the
    PascalCase signal names. ``pid`` rides the adoption record for the
    2b/5-4 ownership trail — no contract signal carries a pid today, so 2b
    drops adoption records on the wire (they stay observable in-process).
    """

    event: str
    job_id: str | None
    epoch: int
    kind: str | None = None
    fraction: float | None = None
    exit_code: int | None = None
    pid: int | None = None


@dataclass(slots=True)
class _JobRecord:
    """Internal per-job state (never leaves the registry)."""

    kind: str
    ttl: float
    deadline: float
    ended: bool = False
    pid: int | None = None


def _validate_kind(kind: object) -> str:
    if not isinstance(kind, str) or not kind:
        raise ValueError(f"job kind must be a non-empty string, got {kind!r}")
    return kind


def _validate_ttl(ttl: object) -> float:
    # NaN/inf/negative/bool/str all fail loud: NaN must never become a
    # never-expiring lease, inf is unrepresentable in contract ttl:u, and a
    # huge int that overflows float must not escape as OverflowError.
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 <= ttl < float("inf"):
        raise ValueError(f"job ttl must be a finite number >= 0 seconds, got {ttl!r}")
    try:
        return float(ttl)
    except OverflowError as exc:
        raise ValueError(f"job ttl out of float range, got {ttl!r}") from exc


def _validate_pid(pid: object) -> int:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError(f"adopted pid must be a positive integer, got {pid!r}")
    return pid


def _validate_fraction(fraction: object) -> float:
    # NaN fails the chained comparison → loud, never recorded. -0.0 is
    # normalized (repr-distinct from 0.0 in the sink otherwise).
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not 0.0 <= fraction <= 1.0
    ):
        raise ValueError(f"progress fraction must be within 0.0–1.0, got {fraction!r}")
    return float(fraction) + 0.0


def _validate_exit_code(exit_code: object) -> int:
    # -1 is reserved: it is the synthetic expiry code
    # (SYNTHETIC_EXPIRY_EXIT_CODE), so an explicit end(job, -1) would be
    # byte-identical to lease expiry in the sink.
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code == -1:
        raise ValueError(f"exit code must be an integer other than -1, got {exit_code!r}")
    return exit_code


class EventHub:
    """Pure job registry with lazy leases and a monotonic epoch.

    Caller contract (all documented, all load-bearing):

    - **Single-threaded caller required.** Check-then-act sequences
      (collision loop, sweep-then-mutate, the ``ended`` flag) assume no
      concurrent callers; a threaded 2b dispatcher must serialize outside.
    - **The sink must not raise.** Records are stored before they are
      appended, so a raising sink leaves registry state ahead of the
      event stream. Infallible by construction here (list append, log
      call); 2b's emitter must preserve that (queue-then-emit or emit
      outside the registry).
    - **Ended records are retained, unbounded, intentionally.** The
      ``JobEnded``-vs-``UnknownJob`` distinction needs them; a future
      ``ForgetJob`` or bounded LRU would change tested semantics and
      belongs to a later story, not a silent optimization.

    Args:
        epoch: this start's epoch (``>= 1``; ``0`` is the reserved
            consumer "never hydrated" sentinel and is rejected). Bumped per
            start by the composition root (in-memory, Gate-1 rec 4).
        clock: monotonic seconds source (production: ``time.monotonic``).
        id_factory: opaque unique id source (production: ``uuid4().hex``).
        sink: every state change is appended here, in order — starting
            with ``jobs_cleared`` FIRST, before any job can exist.
        control_allowlist: kind → allowed actions (defaults to a copy of
            :data:`CONTROL_ALLOWLIST`).
    """

    def __init__(
        self,
        *,
        epoch: int,
        clock: Callable[[], float],
        id_factory: Callable[[], str],
        sink: Callable[[HubEvent], None],
        control_allowlist: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError(f"hub epoch must be an integer >= 1, got {epoch!r}")
        self._epoch = epoch
        self._clock = clock
        self._id_factory = id_factory
        self._sink = sink
        self._allowlist: Mapping[str, frozenset[str]] = (
            dict(control_allowlist) if control_allowlist is not None else dict(CONTROL_ALLOWLIST)
        )
        self._jobs: dict[str, _JobRecord] = {}
        self._sink(HubEvent(event="jobs_cleared", job_id=None, epoch=epoch))

    @property
    def epoch(self) -> int:
        """This start's epoch (bumped per start, never reused)."""
        return self._epoch

    def begin(self, kind: str, ttl: float) -> str:
        """Allocate a live job (wire: ``BeginJob(kind, ttl) → job_id``)."""
        self._sweep()
        validated_kind = _validate_kind(kind)
        validated_ttl = _validate_ttl(ttl)
        job_id = self._fresh_id()
        self._jobs[job_id] = _JobRecord(
            kind=validated_kind,
            ttl=validated_ttl,
            deadline=self._clock() + validated_ttl,
        )
        self._sink(
            HubEvent(event="job_started", job_id=job_id, epoch=self._epoch, kind=validated_kind)
        )
        return job_id

    def renew(self, job_id: str) -> None:
        """Extend a live job's lease by its original ttl (wire: ``RenewJob``)."""
        now = self._clock()
        self._sweep(now)
        record = self._require_live(job_id)
        record.deadline = now + record.ttl

    def adopt(self, job_id: str, pid: int) -> None:
        """Record the observed child PID (wire: ``AdoptJob``; AD-37).

        Adopting twice updates the recorded PID (a wrapper handing off the
        observed child replaces, never duplicates). Identity before value:
        unknown/ended jobs raise before the pid is validated.
        """
        self._sweep()
        record = self._require_live(job_id)
        record.pid = _validate_pid(pid)
        self._sink(HubEvent(event="job_adopted", job_id=job_id, epoch=self._epoch, pid=record.pid))

    def report_progress(self, job_id: str, fraction: float) -> None:
        """Record progress (wire: ``ReportProgress``).

        Out of range is a domain :class:`ValueError` (Gate-1 rec 6 — the
        contract's typed list has no ``BadFraction``); unknown/ended jobs
        raise the typed errors first (identity before value).
        """
        self._sweep()
        self._require_live(job_id)
        validated_fraction = _validate_fraction(fraction)
        self._sink(
            HubEvent(
                event="job_progress",
                job_id=job_id,
                epoch=self._epoch,
                fraction=validated_fraction,
            )
        )

    def end(self, job_id: str, exit_code: int) -> None:
        """Mark a job ended (wire: ``EndJob``). A second end → ``JobEnded``."""
        self._sweep()
        record = self._require_live(job_id)
        validated_exit = _validate_exit_code(exit_code)
        record.ended = True
        self._sink(
            HubEvent(
                event="job_finished",
                job_id=job_id,
                epoch=self._epoch,
                exit_code=validated_exit,
            )
        )

    def control(self, job_id: str, action: str) -> None:
        """Check an action against the kind allowlist (wire: ``Control``).

        The single control path (H1): a UI never shells the tool directly.
        Checking changes no state, so nothing is recorded — 2b wires action
        delivery around this check.
        """
        self._sweep()
        record = self._require_live(job_id)
        if not isinstance(action, str) or action not in self._allowlist.get(
            record.kind, frozenset()
        ):
            raise NotControllable(job_id, action)

    def active_jobs(self) -> dict[str, str]:
        """``{job_id: kind}`` for live jobs only (2b's ``GetActiveJobs``).

        Expired-but-unswept jobs are expired lazily first, so they never
        appear — with their synthetic finishes recorded in the sink.
        """
        self._sweep()
        return {job_id: record.kind for job_id, record in self._jobs.items() if not record.ended}

    def _require_live(self, job_id: object) -> _JobRecord:
        """Return the live record or raise the typed error (sweep first)."""
        if not isinstance(job_id, str):
            raise UnknownJob(job_id)
        record = self._jobs.get(job_id)
        if record is None:
            raise UnknownJob(job_id)
        if record.ended:
            raise JobEnded(job_id)
        return record

    def _fresh_id(self) -> str:
        """Allocate an id the registry does not hold (bounded retries).

        A factory returning non-string, empty, or duplicate ids is a
        programming error — fail loud instead of storing unreachable jobs
        or looping forever.
        """
        for _ in range(100):
            job_id = self._id_factory()
            if isinstance(job_id, str) and job_id and job_id not in self._jobs:
                return job_id
        raise RuntimeError("id_factory returned no unique id in 100 attempts")

    def _sweep(self, now: float | None = None) -> None:
        """Expire past-deadline live jobs with synthetic finishes (lazy).

        Runs at every public entry: no background thread, no timer, no
        sweep loop. ``now >= deadline`` expires (a ``ttl=0`` job is stored
        live by ``begin`` and expires on the next call); finishes record in
        insertion order before the invoking call proceeds. Iterates a
        snapshot so a re-entrant sink (one that calls back into the hub)
        cannot crash the loop.
        """
        current = self._clock() if now is None else now
        for job_id, record in list(self._jobs.items()):
            if not record.ended and current >= record.deadline:
                record.ended = True
                self._sink(
                    HubEvent(
                        event="job_finished",
                        job_id=job_id,
                        epoch=self._epoch,
                        exit_code=SYNTHETIC_EXPIRY_EXIT_CODE,
                    )
                )
