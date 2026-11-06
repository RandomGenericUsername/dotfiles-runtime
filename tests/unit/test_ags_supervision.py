"""Unit tests for the RFC §4.3 AGS supervision probe and safe repair."""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.adapters.ags_supervision import (
    ALWAYS_ON_APPS,
    AgsSupervisionProbe,
    AgsSupervisionRepair,
    CommandResult,
    OrphanProcess,
    ProcessInfo,
    SupervisionReport,
    UnitSupervision,
)
from runtime.domain.ags_apps import AppAppearanceState, AppliedAppearance

BAR = ALWAYS_ON_APPS[0]
NOTIFICATIONS = ALWAYS_ON_APPS[1]


class _Runner:
    """Scripted command runner keyed by exact argv."""

    def __init__(self, responses: dict[tuple[str, ...], CommandResult | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: object) -> CommandResult:
        key = tuple(argv)  # type: ignore[arg-type]
        self.calls.append(key)
        response = self.responses.get(key, CommandResult(1, "", "unexpected command"))
        if isinstance(response, Exception):
            raise response
        return response


def _healthy_responses() -> dict[tuple[str, ...], CommandResult]:
    return {
        ("systemctl", "--user", "is-active", BAR.unit): CommandResult(0, "active\n"),
        ("systemctl", "--user", "is-active", NOTIFICATIONS.unit): CommandResult(0, "active\n"),
        ("systemctl", "--user", "show", "-p", "MainPID", "--value", BAR.unit): CommandResult(
            0, "100\n"
        ),
        (
            "systemctl",
            "--user",
            "show",
            "-p",
            "MainPID",
            "--value",
            NOTIFICATIONS.unit,
        ): CommandResult(0, "200\n"),
        ("ags", "list"): CommandResult(0, "ags\nnotifications\n"),
        ("busctl", "--user", "status", BAR.bus_name): CommandResult(
            0, f"PID=101\nCGroup={BAR.unit}\n"
        ),
        (
            "busctl",
            "--user",
            "status",
            NOTIFICATIONS.bus_name,
        ): CommandResult(0, f"PID=201\nCGroup={NOTIFICATIONS.unit}\n"),
    }


def _healthy_cgroups() -> dict[int, str]:
    return {
        100: f"0::/user.slice/user@1000.service/{BAR.unit}",
        101: f"0::/user.slice/user@1000.service/{BAR.unit}",
        200: f"0::/user.slice/user@1000.service/{NOTIFICATIONS.unit}",
        201: f"0::/user.slice/user@1000.service/{NOTIFICATIONS.unit}",
    }


def _probe(
    responses: dict[tuple[str, ...], CommandResult | Exception],
    *,
    cgroups: dict[int, str] | None = None,
    processes: list[ProcessInfo] | None = None,
    install_spine: Path | None = None,
) -> tuple[AgsSupervisionProbe, _Runner]:
    runner = _Runner(responses)
    cgroup_map = _healthy_cgroups() if cgroups is None else cgroups
    probe = AgsSupervisionProbe(
        install_spine=install_spine,
        command_runner=runner,
        process_lister=lambda: processes or [],
        cgroup_reader=lambda pid: cgroup_map.get(pid),
        pid_alive=lambda pid: pid in {100, 200},
    )
    return probe, runner


class TestUnitState:
    def test_active_units_with_owner_inside_cgroup_are_healthy(self) -> None:
        probe, _ = _probe(_healthy_responses())
        report = probe.inspect()
        assert report.available is True
        assert report.healthy is True
        assert {unit.app for unit in report.units} == {"bar", "notifications"}
        assert all(unit.main_pid_alive for unit in report.units)
        assert report.orphans == ()

    def test_inactive_unit_is_drift(self) -> None:
        responses = _healthy_responses()
        responses[("systemctl", "--user", "is-active", BAR.unit)] = CommandResult(3, "inactive\n")
        probe, _ = _probe(responses)
        report = probe.inspect()
        bar = next(unit for unit in report.units if unit.app == "bar")
        assert bar.active is False
        assert bar.healthy is False
        assert "inactive" in bar.detail
        assert report.healthy is False

    def test_pid_mismatch_outside_unit_cgroup_is_drift(self) -> None:
        responses = _healthy_responses()
        responses[("busctl", "--user", "status", BAR.bus_name)] = CommandResult(0, "PID=999\n")
        probe, _ = _probe(
            responses,
            cgroups={
                100: f"0::/user.slice/{BAR.unit}",
                999: "0::/user.slice/session-other.scope",
            },
        )
        report = probe.inspect()
        bar = next(unit for unit in report.units if unit.app == "bar")
        assert bar.main_pid == 100
        assert bar.bus_owner_pid == 999
        assert bar.bus_owner_in_unit is False
        assert bar.healthy is False
        assert "outside the unit cgroup" in bar.detail

    def test_absent_systemd_is_a_skip_not_a_failure(self) -> None:
        probe, _ = _probe(
            {("systemctl", "--user", "is-active", BAR.unit): FileNotFoundError("systemctl")}
        )
        report = probe.inspect()
        assert report.available is False
        assert "skipped" in report.note
        assert report.units == ()
        assert report.healthy is False

    def test_no_user_session_is_a_skip_not_a_failure(self) -> None:
        probe, _ = _probe(
            {
                ("systemctl", "--user", "is-active", BAR.unit): CommandResult(
                    1, "", "Failed to connect to bus: No medium found\n"
                )
            }
        )
        report = probe.inspect()
        assert report.available is False
        assert "No medium found" in report.note
        assert "skipped" in report.note


class TestOrphans:
    def test_wrapper_and_gjs_outside_the_unit_are_orphans(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        config_dir = spine / "config" / BAR.config_dir
        processes = [
            ProcessInfo(pid=300, argv=("ags", "run", "-d", str(config_dir)), ppid=1),
            ProcessInfo(pid=301, argv=("gjs", "-m", "/run/user/1000/ags.js"), ppid=300),
        ]
        probe, _ = _probe(
            _healthy_responses(),
            cgroups={
                100: f"0::/{BAR.unit}",
                101: f"0::/{BAR.unit}",
                300: "0::/user.slice/session-other.scope",
            },
            processes=processes,
            install_spine=spine,
        )
        report = probe.inspect()
        assert {(orphan.pid, orphan.kind) for orphan in report.orphans} == {
            (300, "ags-run"),
            (301, "gjs"),
        }
        assert {orphan.instance for orphan in report.orphans} == {BAR.instance}
        assert report.healthy is False

    def test_wrapper_inside_the_unit_is_not_an_orphan(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        config_dir = spine / "config" / BAR.config_dir
        processes = [
            ProcessInfo(pid=300, argv=("ags", "run", "-d", str(config_dir)), ppid=1),
            ProcessInfo(pid=301, argv=("gjs", "-m", "/run/user/1000/ags.js"), ppid=300),
        ]
        cgroups = _healthy_cgroups()
        cgroups[300] = f"0::/{BAR.unit}"
        probe, _ = _probe(
            _healthy_responses(),
            cgroups=cgroups,
            processes=processes,
            install_spine=spine,
        )
        assert probe.inspect().orphans == ()


class TestAppearanceFreshness:
    def _applied(self) -> AppliedAppearance:
        return AppliedAppearance(
            version=1,
            appearance_hash="new",
            operation_id="op",
            completed_at="2026-11-06T00:00:00Z",
            apps={
                BAR.instance: AppAppearanceState(
                    app=BAR.app,
                    instance=BAR.instance,
                    mode="live",
                    status="applied",
                    appearance_hash="old",
                ),
                NOTIFICATIONS.instance: AppAppearanceState(
                    app=NOTIFICATIONS.app,
                    instance=NOTIFICATIONS.instance,
                    mode="live",
                    status="applied",
                    appearance_hash="new",
                ),
            },
        )

    def test_older_ledger_entry_is_stale(self) -> None:
        probe, _ = _probe(_healthy_responses())
        report = probe.inspect(expected_appearance_hash="new", applied=self._applied())
        bar = next(unit for unit in report.units if unit.app == "bar")
        notifications = next(unit for unit in report.units if unit.app == "notifications")
        assert bar.stale_appearance is True
        assert bar.healthy is False
        assert notifications.stale_appearance is False
        assert notifications.healthy is True

    def test_no_ledger_is_unknown_not_stale(self) -> None:
        probe, _ = _probe(_healthy_responses())
        report = probe.inspect(expected_appearance_hash="new", applied=None)
        assert all(unit.stale_appearance is None for unit in report.units)
        assert report.healthy is True


def _unit(
    app: object,
    *,
    active: bool,
    main_pid: int,
    owner: int | None,
    owner_in_unit: bool | None,
    healthy: bool,
) -> UnitSupervision:
    identity = app  # typed as AlwaysOnApp by construction in the caller
    return UnitSupervision(
        app=identity.app,  # type: ignore[attr-defined]
        unit=identity.unit,  # type: ignore[attr-defined]
        instance=identity.instance,  # type: ignore[attr-defined]
        active=active,
        main_pid=main_pid,
        main_pid_alive=main_pid > 0,
        bus_owner_pid=owner,
        bus_owner_in_unit=owner_in_unit,
        instance_live=owner is not None,
        stale_appearance=None,
        healthy=healthy,
        detail="test",
    )


class TestRepairActions:
    def test_inactive_and_mismatched_units_get_only_restart_and_quit(self) -> None:
        report = SupervisionReport(
            available=True,
            units=(
                _unit(
                    BAR,
                    active=False,
                    main_pid=0,
                    owner=None,
                    owner_in_unit=None,
                    healthy=False,
                ),
                _unit(
                    NOTIFICATIONS,
                    active=True,
                    main_pid=200,
                    owner=999,
                    owner_in_unit=False,
                    healthy=False,
                ),
            ),
            orphans=(
                OrphanProcess(
                    pid=301,
                    kind="gjs",
                    app=NOTIFICATIONS.app,
                    unit=NOTIFICATIONS.unit,
                    instance=NOTIFICATIONS.instance,
                    config_dir=NOTIFICATIONS.config_dir,
                    argv=("gjs", "-m", "/run/user/1000/ags.js"),
                ),
            ),
        )
        runner = _Runner({})
        actions = AgsSupervisionRepair(command_runner=runner).repair(report)

        assert [(action.kind, action.target) for action in actions] == [
            ("restart", BAR.unit),
            ("quit", NOTIFICATIONS.instance),
            ("restart", NOTIFICATIONS.unit),
        ]
        allowed = {
            ("quit", NOTIFICATIONS.instance),
            ("restart", BAR.unit),
            ("restart", NOTIFICATIONS.unit),
        }
        assert all((action.kind, action.target) in allowed for action in actions)
        assert all(
            argv[:2] == ("ags", "quit") or argv[:3] == ("systemctl", "--user", "restart")
            for argv in runner.calls
        )
        assert not any("kill" in argv or "pkill" in argv for argv in runner.calls)

    def test_unavailable_systemd_repairs_nothing(self) -> None:
        runner = _Runner({})
        actions = AgsSupervisionRepair(command_runner=runner).repair(
            SupervisionReport(available=False, note="skipped")
        )
        assert actions == ()
        assert runner.calls == []

    def test_stale_appearance_only_restarts(self) -> None:
        stale = _unit(
            BAR,
            active=True,
            main_pid=100,
            owner=None,
            owner_in_unit=None,
            healthy=False,
        )
        report = SupervisionReport(
            available=True,
            units=(stale,),
        )
        runner = _Runner({})
        actions = AgsSupervisionRepair(command_runner=runner).repair(report)
        assert [(action.kind, action.target) for action in actions] == [("restart", BAR.unit)]
        assert runner.calls == [("systemctl", "--user", "restart", BAR.unit)]


def test_bus_owner_not_listed_by_ags_is_drift() -> None:
    responses = _healthy_responses()
    responses[("ags", "list")] = CommandResult(0, "ags\n")
    probe, _ = _probe(responses)
    report = probe.inspect()
    notifications = next(unit for unit in report.units if unit.app == "notifications")
    assert notifications.instance_live is False
    assert notifications.bus_owner_pid is None
    assert notifications.healthy is False


@pytest.mark.parametrize("rc", [1, 2, 127])
def test_inactive_returncodes_stay_inactive(rc: int) -> None:
    responses = _healthy_responses()
    responses[("systemctl", "--user", "is-active", BAR.unit)] = CommandResult(rc, "inactive\n")
    probe, _ = _probe(responses)
    bar = next(unit for unit in probe.inspect().units if unit.app == "bar")
    assert bar.active is False
