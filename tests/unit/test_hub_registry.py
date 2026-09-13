"""Unit tests for P5-1-2a hub domain core (registry + leases + epoch).

Transport-free by construction: the clock and id factory are fakes, the
sink is a list — no live bus, no timers, no threads (AC 8). The trailing
class pins the 2a "no wire" invariant (no dbus/gi/dasbus/jeepney import
anywhere); P5-1-2b relaxes it to adapters-only.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from runtime.domain.hub import (
    CONTROL_ALLOWLIST,
    SYNTHETIC_EXPIRY_EXIT_CODE,
    EventHub,
    HubEvent,
)
from runtime.domain.models import (
    JobEnded,
    NotControllable,
    UnknownJob,
)


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _hub(
    clock: _Clock | None = None,
    sink: list[HubEvent] | None = None,
    *,
    epoch: int = 1,
    **kwargs: object,
) -> tuple[EventHub, _Clock, list[HubEvent]]:
    clock = clock if clock is not None else _Clock()
    events: list[HubEvent] = sink if sink is not None else []
    counter = itertools.count(1)
    hub = EventHub(
        epoch=epoch,
        clock=clock,
        id_factory=lambda: f"job-{next(counter)}",
        sink=events.append,
        **kwargs,  # type: ignore[arg-type]
    )
    return hub, clock, events


def _kinds(events: list[HubEvent]) -> list[str]:
    return [event.event for event in events]


class TestStart:
    def test_start_records_jobs_cleared_first(self) -> None:
        _, _, events = _hub(epoch=7)
        assert events == [HubEvent(event="jobs_cleared", job_id=None, epoch=7)]

    def test_epoch_zero_and_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="epoch"):
            _hub(epoch=0)
        with pytest.raises(ValueError, match="epoch"):
            _hub(epoch=-3)

    def test_every_record_carries_epoch(self) -> None:
        hub, _, events = _hub(epoch=4)
        job = hub.begin("capture", 60)
        hub.report_progress(job, 0.5)
        hub.end(job, 0)
        assert events[0].event == "jobs_cleared"
        assert all(event.epoch == 4 for event in events)
        assert len(events) == 4


class TestLifecycle:
    def test_begin_allocates_unique_ids(self) -> None:
        hub, _, events = _hub()
        first = hub.begin("capture", 60)
        second = hub.begin("speedtest", 60)
        assert first != second
        assert [e.job_id for e in events if e.event == "job_started"] == [first, second]
        assert events[1].kind == "capture"

    def test_begin_rejects_bad_kind_and_ttl(self) -> None:
        hub, _, _ = _hub()
        with pytest.raises(ValueError, match="kind"):
            hub.begin("", 60)
        with pytest.raises(ValueError, match="ttl"):
            hub.begin("capture", -1)
        with pytest.raises(ValueError, match="ttl"):
            hub.begin("capture", float("nan"))
        with pytest.raises(ValueError, match="ttl"):
            hub.begin("capture", float("inf"))

    def test_renew_extends_by_original_ttl(self) -> None:
        hub, clock, _ = _hub()
        job = hub.begin("capture", 10)
        clock.advance(9)
        hub.renew(job)  # deadline now 1009 + 10
        clock.advance(5)  # t=1014 < 1019: still live
        assert hub.active_jobs() == {job: "capture"}
        clock.advance(10)  # t=1024 > 1019: expired
        assert hub.active_jobs() == {}

    def test_renew_unknown_is_unknown_job(self) -> None:
        hub, _, _ = _hub()
        with pytest.raises(UnknownJob):
            hub.renew("job-nope")

    def test_use_after_end_is_job_ended(self) -> None:
        hub, _, _ = _hub()
        job = hub.begin("capture", 60)
        hub.end(job, 0)
        with pytest.raises(JobEnded):
            hub.renew(job)
        with pytest.raises(JobEnded):
            hub.report_progress(job, 0.5)
        with pytest.raises(JobEnded):
            hub.end(job, 0)  # second end, not UnknownJob
        with pytest.raises(JobEnded):
            hub.adopt(job, 123)

    def test_end_records_exit_code(self) -> None:
        hub, _, events = _hub()
        job = hub.begin("capture", 60)
        hub.end(job, 3)
        assert events[-1] == HubEvent(event="job_finished", job_id=job, epoch=1, exit_code=3)

    def test_end_rejects_non_integer_exit(self) -> None:
        hub, _, _ = _hub()
        job = hub.begin("capture", 60)
        with pytest.raises(ValueError, match="exit code"):
            hub.end(job, "0")  # type: ignore[arg-type]

    def test_report_progress_validates_range(self) -> None:
        hub, _, events = _hub()
        job = hub.begin("capture", 60)
        hub.report_progress(job, 0.0)
        hub.report_progress(job, 1.0)
        with pytest.raises(ValueError, match="fraction"):
            hub.report_progress(job, 1.5)
        with pytest.raises(ValueError, match="fraction"):
            hub.report_progress(job, float("nan"))
        assert events[-1].fraction == 1.0

    def test_report_progress_unknown_id_is_unknown_job(self) -> None:
        hub, _, _ = _hub()
        with pytest.raises(UnknownJob):
            hub.report_progress("job-nope", 0.5)

    def test_end_rejects_reserved_minus_one(self) -> None:
        """-1 is the synthetic expiry code — never an explicit end."""
        hub, _, _ = _hub()
        job = hub.begin("capture", 60)
        with pytest.raises(ValueError, match="-1"):
            hub.end(job, -1)
        assert hub.active_jobs() == {job: "capture"}

    def test_non_string_job_id_is_unknown_job(self) -> None:
        hub, _, _ = _hub()
        with pytest.raises(UnknownJob):
            hub.renew(123)  # type: ignore[arg-type]

    def test_duplicate_id_factory_stays_unique(self) -> None:
        clock = _Clock()
        events: list[HubEvent] = []
        ids = iter(["dup", "dup", "dup2"])
        hub = EventHub(epoch=1, clock=clock, id_factory=lambda: next(ids), sink=events.append)
        first = hub.begin("capture", 60)
        second = hub.begin("capture", 60)
        assert (first, second) == ("dup", "dup2")

    def test_non_string_factory_output_fails_loud(self) -> None:
        clock = _Clock()
        events: list[HubEvent] = []
        hub = EventHub(epoch=1, clock=clock, id_factory=lambda: 123, sink=events.append)  # type: ignore[return-value]
        with pytest.raises(RuntimeError, match="id_factory"):
            hub.begin("capture", 60)


class TestLazyExpiry:
    def test_expiry_emits_synthetic_finish_before_call(self) -> None:
        hub, clock, events = _hub()
        first = hub.begin("capture", 10)
        second = hub.begin("speedtest", 30)
        clock.advance(11)
        assert hub.active_jobs() == {second: "speedtest"}
        finishes = [e for e in events if e.event == "job_finished"]
        assert [(e.job_id, e.exit_code) for e in finishes] == [(first, SYNTHETIC_EXPIRY_EXIT_CODE)]
        assert finishes[0].epoch == 1
        assert SYNTHETIC_EXPIRY_EXIT_CODE == -1

    def test_expire_then_renew_is_job_ended(self) -> None:
        hub, clock, _ = _hub()
        job = hub.begin("capture", 10)
        clock.advance(11)
        with pytest.raises(JobEnded):
            hub.renew(job)

    def test_expire_then_end_is_job_ended(self) -> None:
        hub, clock, _ = _hub()
        job = hub.begin("capture", 10)
        clock.advance(11)
        with pytest.raises(JobEnded):
            hub.end(job, 0)

    def test_zero_ttl_never_survives(self) -> None:
        hub, _, _ = _hub()
        hub.begin("capture", 0)
        assert hub.active_jobs() == {}


class TestAdopt:
    def test_adopt_records_pid_and_updates(self) -> None:
        hub, _, events = _hub()
        job = hub.begin("capture", 60)
        hub.adopt(job, 4242)
        hub.adopt(job, 4343)
        adopted = [e for e in events if e.event == "job_adopted"]
        assert [e.pid for e in adopted] == [4242, 4343]
        assert all(e.job_id == job and e.epoch == 1 for e in adopted)

    def test_adopt_unknown_and_ended(self) -> None:
        hub, _, _ = _hub()
        with pytest.raises(UnknownJob):
            hub.adopt("job-nope", 1)
        job = hub.begin("capture", 60)
        hub.end(job, 0)
        with pytest.raises(JobEnded):
            hub.adopt(job, 1)

    def test_adopt_after_expiry_is_job_ended(self) -> None:
        hub, clock, _ = _hub()
        job = hub.begin("capture", 10)
        clock.advance(11)
        with pytest.raises(JobEnded):
            hub.adopt(job, 1)

    def test_adopt_rejects_bad_pid(self) -> None:
        hub, _, _ = _hub()
        job = hub.begin("capture", 60)
        with pytest.raises(ValueError, match="pid"):
            hub.adopt(job, 0)
        with pytest.raises(ValueError, match="pid"):
            hub.adopt(job, -5)


class TestControl:
    def test_allowlisted_action_passes(self) -> None:
        hub, _, _ = _hub()
        job = hub.begin("capture", 60)
        hub.control(job, "pause")
        hub.control(job, "resume")
        hub.control(job, "stop")

    def test_denied_action_is_not_controllable(self) -> None:
        hub, _, _ = _hub()
        job = hub.begin("capture", 60)
        with pytest.raises(NotControllable):
            hub.control(job, "explode")

    def test_unknown_kind_denies_all(self) -> None:
        hub, _, _ = _hub()
        job = hub.begin("speedtest", 60)
        with pytest.raises(NotControllable):
            hub.control(job, "stop")

    def test_control_unknown_and_ended(self) -> None:
        hub, _, _ = _hub()
        with pytest.raises(UnknownJob):
            hub.control("job-nope", "stop")
        job = hub.begin("capture", 60)
        hub.end(job, 0)
        with pytest.raises(JobEnded):
            hub.control(job, "stop")

    def test_control_on_expired_unswept_is_job_ended(self) -> None:
        """Sweep-first ordering: expiry beats the allowlist check."""
        hub, clock, _ = _hub()
        job = hub.begin("capture", 10)
        clock.advance(11)
        with pytest.raises(JobEnded):
            hub.control(job, "stop")

    def test_control_unhashable_action_is_not_controllable(self) -> None:
        hub, _, _ = _hub()
        job = hub.begin("capture", 60)
        with pytest.raises(NotControllable):
            hub.control(job, ["pause"])  # type: ignore[arg-type]

    def test_allowlist_table_shape(self) -> None:
        assert CONTROL_ALLOWLIST["capture"] == frozenset({"pause", "resume", "stop"})


class TestActiveJobs:
    def test_hydration_view_excludes_ended_and_expired(self) -> None:
        hub, clock, _ = _hub()
        live = hub.begin("capture", 60)
        short = hub.begin("speedtest", 10)
        done = hub.begin("capture", 60)
        hub.end(done, 0)
        clock.advance(11)
        assert hub.active_jobs() == {live: "capture"}
        assert short not in hub.active_jobs()

    def test_fresh_hub_hydrates_empty(self) -> None:
        hub, _, _ = _hub()
        assert hub.active_jobs() == {}

    def test_stale_id_on_new_hub_is_unknown_job(self) -> None:
        """Restart invalidation: a new hub instance knows no prior ids."""
        old, _, _ = _hub(epoch=1)
        stale = old.begin("capture", 60)
        new, _, _ = _hub(epoch=2)
        with pytest.raises(UnknownJob):
            new.renew(stale)


class TestDaemonWiring:
    def test_build_hub_bumps_epoch(self) -> None:
        import runtime.cli.main as cli_main

        first = cli_main._build_hub()
        second = cli_main._build_hub()
        assert second.epoch > first.epoch >= 1


class TestNoWireIn2a:
    """P5-1-2a invariant: zero D-Bus imports anywhere (2b relaxes this to
    adapters-only — update this test then, not before)."""

    def test_no_dbus_imports(self) -> None:
        roots = [
            Path(__file__).resolve().parents[2] / "src" / "runtime",
        ]
        offenders: list[str] = []
        for root in roots:
            for path in sorted(root.rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    names: list[str] = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module]
                    for name in names:
                        base = name.split(".")[0].lower()
                        if base in {"dbus", "gi", "dasbus", "jeepney", "sdbus", "dbus_fast"}:
                            offenders.append(f"{path}:{node.lineno}: {name}")
        assert offenders == []
