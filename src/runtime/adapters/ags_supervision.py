"""Live supervision of the always-on AGS ``systemd --user`` units (RFC §4.3).

Provisioning owns the static unit contract (`dotfiles-provision` REQ-023); this
adapter is the runtime's read-only live leg and the narrowly scoped repair:

* unit active state via ``systemctl --user is-active``;
* ``MainPID`` liveness and the AGS D-Bus owner PID for the instance;
* orphan ``ags run`` wrappers for an always-on configDir outside the unit
  cgroup, plus their ``gjs`` descendants;
* a note when the appearance ledger says the running app predates the current
  generation.

Absent systemd/a user session is an explicit ``available=False`` report, not a
failure. Repair is limited to ``systemctl --user restart`` and ``ags quit -i``
(RFC §4.2): this module never signals a PID, never ``kill``/``pkill``s and never
walks a process tree with a signal.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from runtime.domain.ags_apps import AppliedAppearance

logger = logging.getLogger(__name__)

__all__ = [
    "ALWAYS_ON_APPS",
    "AgsSupervisionProbe",
    "AgsSupervisionRepair",
    "AlwaysOnApp",
    "CommandResult",
    "OrphanProcess",
    "ProcessInfo",
    "SupervisionAction",
    "SupervisionReport",
    "UnitSupervision",
]


@dataclass(frozen=True, slots=True)
class AlwaysOnApp:
    """One always-on AGS identity (unit, instance, configDir, bus name)."""

    app: str
    unit: str
    instance: str
    config_dir: str
    bus_name: str


#: The two always-on apps frozen by the appearance-plane v2 RFC (§4). The
#: bar's AGS instance is ``io.Astal.ags``; the notification overlay also owns
#: the freedesktop notification name.
ALWAYS_ON_APPS: tuple[AlwaysOnApp, ...] = (
    AlwaysOnApp(
        app="bar",
        unit="dotfiles-ags-bar.service",
        instance="ags",
        config_dir="ags",
        bus_name="io.Astal.ags",
    ),
    AlwaysOnApp(
        app="notifications",
        unit="dotfiles-ags-notifications.service",
        instance="notifications",
        config_dir="ags-notifications",
        bus_name="org.freedesktop.Notifications",
    ),
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The stdout/stderr/returncode shape the command seams return."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str]], CommandResult]


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """One process snapshot used by orphan detection."""

    pid: int
    argv: tuple[str, ...]
    ppid: int = 0


@dataclass(frozen=True, slots=True)
class UnitSupervision:
    """Live supervision verdict for one always-on app."""

    app: str
    unit: str
    instance: str
    active: bool
    main_pid: int
    main_pid_alive: bool
    bus_owner_pid: int | None
    bus_owner_in_unit: bool | None
    instance_live: bool | None
    stale_appearance: bool | None
    healthy: bool
    detail: str


@dataclass(frozen=True, slots=True)
class OrphanProcess:
    """An AGS wrapper/gjs process outside its always-on unit cgroup."""

    pid: int
    kind: Literal["ags-run", "gjs"]
    app: str
    unit: str
    instance: str
    config_dir: str
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SupervisionReport:
    """Result of one live supervision pass."""

    available: bool
    note: str = ""
    units: tuple[UnitSupervision, ...] = ()
    orphans: tuple[OrphanProcess, ...] = ()

    @property
    def healthy(self) -> bool:
        """True when systemd was available and every unit/orphan is clean."""
        return self.available and all(unit.healthy for unit in self.units) and not self.orphans


@dataclass(frozen=True, slots=True)
class SupervisionAction:
    """One executed repair action (exactly restart/quit, never a kill)."""

    kind: Literal["quit", "restart"]
    app: str
    target: str
    argv: tuple[str, ...]
    returncode: int


#: Markers that mean there is no reachable user systemd manager. The leg is
#: skipped, never reported as desktop drift.
_NO_SESSION_MARKERS: tuple[str, ...] = (
    "failed to connect to bus",
    "failed to get d-bus connection",
    "no medium found",
    "cannot connect to bus",
    "system has not been booted with systemd",
    "not been booted with systemd",
    "connection refused",
    "timed out",
)


def _run_command(argv: Sequence[str]) -> CommandResult:
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(returncode=124, stderr=f"timed out: {exc}")
    return CommandResult(
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def _scan_processes(proc_root: Path | None = None) -> list[ProcessInfo]:
    """Scan ``/proc`` for argv + PPid snapshots (never signals anything)."""
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    processes: list[ProcessInfo] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = tuple(part for part in raw.decode("utf-8", "replace").split("\0") if part)
        if not argv:
            continue
        ppid = 0
        try:
            for line in (entry / "status").read_text(encoding="utf-8").splitlines():
                if line.startswith("PPid:"):
                    ppid = int(line.split()[1])
                    break
        except OSError, ValueError, IndexError:
            ppid = 0
        processes.append(ProcessInfo(pid=int(entry.name), argv=argv, ppid=ppid))
    return processes


def _read_cgroup(pid: int, proc_root: Path | None = None) -> str | None:
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        return (root / str(pid) / "cgroup").read_text(encoding="utf-8")
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_ags_run(argv: Sequence[str]) -> bool:
    return len(argv) >= 2 and Path(argv[0]).name == "ags" and argv[1] == "run"


def _is_gjs(argv: Sequence[str]) -> bool:
    return bool(argv) and Path(argv[0]).name.startswith("gjs")


def _ags_config_dir(argv: Sequence[str]) -> Path | None:
    for index, arg in enumerate(argv):
        if arg in ("-d", "--directory") and index + 1 < len(argv):
            return Path(argv[index + 1])
    return None


def _in_unit_cgroup(cgroup: str | None, unit: str) -> bool | None:
    if cgroup is None:
        return None
    return re.search(rf"(^|/){re.escape(unit)}(/|$)", cgroup, re.MULTILINE) is not None


class AgsSupervisionProbe:
    """Read-only live supervision probe with injectable command/proc seams."""

    def __init__(
        self,
        *,
        apps: Sequence[AlwaysOnApp] = ALWAYS_ON_APPS,
        install_spine: Path | None = None,
        command_runner: CommandRunner | None = None,
        process_lister: Callable[[], Sequence[ProcessInfo]] | None = None,
        cgroup_reader: Callable[[int], str | None] | None = None,
        pid_alive: Callable[[int], bool] | None = None,
        systemctl: str = "systemctl",
        ags: str = "ags",
        busctl: str = "busctl",
        proc_root: Path | None = None,
    ) -> None:
        self._apps = tuple(apps)
        self._install_spine = install_spine
        self._run = command_runner or _run_command
        self._process_lister = process_lister or (lambda: _scan_processes(proc_root))
        self._cgroup_reader = cgroup_reader or (lambda pid: _read_cgroup(pid, proc_root))
        self._pid_alive = pid_alive or _pid_alive
        self._systemctl = systemctl
        self._ags = ags
        self._busctl = busctl

    @property
    def apps(self) -> tuple[AlwaysOnApp, ...]:
        return self._apps

    def inspect(
        self,
        *,
        expected_appearance_hash: str | None = None,
        applied: AppliedAppearance | None = None,
    ) -> SupervisionReport:
        """Probe both units, their bus owners, orphans and freshness."""
        active: dict[str, bool] = {}
        for app in self._apps:
            try:
                result = self._run([self._systemctl, "--user", "is-active", app.unit])
            except (FileNotFoundError, OSError) as exc:
                return SupervisionReport(
                    available=False,
                    note=f"systemctl unavailable: {exc}; supervision checks skipped",
                )
            note = self._unavailable_note(result)
            if note is not None:
                return SupervisionReport(
                    available=False,
                    note=f"{note}; supervision checks skipped",
                )
            active[app.app] = result.returncode == 0 and result.stdout.strip() == "active"

        processes = list(self._process_lister())
        orphans = self._orphans(processes)

        units: list[UnitSupervision] = []
        for app in self._apps:
            units.append(
                self._unit_status(
                    app,
                    active=active[app.app],
                    expected_appearance_hash=expected_appearance_hash,
                    applied=applied,
                )
            )
        return SupervisionReport(
            available=True,
            units=tuple(units),
            orphans=tuple(orphans),
        )

    @staticmethod
    def _unavailable_note(result: CommandResult) -> str | None:
        text = f"{result.stdout}\n{result.stderr}".lower()
        for marker in _NO_SESSION_MARKERS:
            if marker in text:
                return (result.stderr or result.stdout).strip() or marker
        return None

    def _unit_status(
        self,
        app: AlwaysOnApp,
        *,
        active: bool,
        expected_appearance_hash: str | None,
        applied: AppliedAppearance | None,
    ) -> UnitSupervision:
        main_pid = self._main_pid(app.unit) if active else 0
        main_pid_alive = main_pid > 0 and self._pid_alive(main_pid)
        instance_live = self._instance_live(app.instance)
        owner_pid, owner_note = (
            (None, "instance is not on the AGS bus")
            if instance_live is False
            else self._bus_owner_pid(app.bus_name)
        )
        owner_in_unit = (
            None if owner_pid is None else _in_unit_cgroup(self._cgroup_reader(owner_pid), app.unit)
        )
        stale = self._stale_appearance(app, expected_appearance_hash, applied)

        healthy = True
        parts: list[str] = []
        if not active:
            healthy = False
            parts.append("unit inactive")
        else:
            if main_pid <= 0:
                healthy = False
                parts.append("unit has no MainPID")
            elif not main_pid_alive:
                healthy = False
                parts.append(f"MainPID {main_pid} is not alive")
            else:
                parts.append(f"active, MainPID {main_pid} alive")

        if active:
            if instance_live is False:
                healthy = False
                parts.append(f"instance {app.instance!r} is not on the AGS bus")
            elif owner_pid is None:
                parts.append(f"bus owner unknown ({owner_note})")
            elif owner_pid == main_pid:
                parts.append(f"bus owner pid {owner_pid} == MainPID")
            elif owner_in_unit is True:
                parts.append(f"bus owner pid {owner_pid} inside the unit cgroup")
            elif owner_in_unit is False:
                healthy = False
                parts.append(f"MainPID {main_pid} != bus owner {owner_pid} outside the unit cgroup")
            else:
                parts.append(f"bus owner pid {owner_pid} (cgroup unknown)")

        if stale is True:
            healthy = False
            parts.append("running app predates the current appearance hash")
        elif stale is None and expected_appearance_hash is not None:
            parts.append("appearance freshness unknown (no ledger record)")

        return UnitSupervision(
            app=app.app,
            unit=app.unit,
            instance=app.instance,
            active=active,
            main_pid=main_pid,
            main_pid_alive=main_pid_alive,
            bus_owner_pid=owner_pid,
            bus_owner_in_unit=owner_in_unit,
            instance_live=instance_live,
            stale_appearance=stale,
            healthy=healthy,
            detail="; ".join(parts),
        )

    def _main_pid(self, unit: str) -> int:
        result = self._call([self._systemctl, "--user", "show", "-p", "MainPID", "--value", unit])
        if result.returncode != 0:
            return 0
        for token in result.stdout.split():
            try:
                return int(token)
            except ValueError:
                continue
        return 0

    def _instance_live(self, instance: str) -> bool | None:
        try:
            result = self._run([self._ags, "list"])
        except FileNotFoundError, OSError:
            return None
        if result.returncode != 0:
            return None
        names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        return instance in names

    def _bus_owner_pid(self, bus_name: str) -> tuple[int | None, str]:
        try:
            result = self._call([self._busctl, "--user", "status", bus_name])
        except (FileNotFoundError, OSError) as exc:
            return None, f"busctl unavailable: {exc}"
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "name is not owned"
            return None, detail
        for line in result.stdout.splitlines():
            if line.startswith("PID="):
                try:
                    return int(line.split("=", 1)[1].strip()), ""
                except ValueError:
                    break
        return None, "busctl status reported no PID"

    def _call(self, argv: Sequence[str]) -> CommandResult:
        try:
            return self._run(argv)
        except (FileNotFoundError, OSError) as exc:
            return CommandResult(returncode=127, stderr=str(exc))

    @staticmethod
    def _stale_appearance(
        app: AlwaysOnApp,
        expected_appearance_hash: str | None,
        applied: AppliedAppearance | None,
    ) -> bool | None:
        if expected_appearance_hash is None or applied is None:
            return None
        record = applied.apps.get(app.instance)
        if record is None:
            return None
        return not (
            record.appearance_hash == expected_appearance_hash and record.status == "applied"
        )

    def _orphans(self, processes: Sequence[ProcessInfo]) -> list[OrphanProcess]:
        by_pid = {process.pid: process for process in processes}
        orphan_wrappers: dict[int, tuple[ProcessInfo, AlwaysOnApp]] = {}
        for process in processes:
            if not _is_ags_run(process.argv):
                continue
            app = self._match_app(_ags_config_dir(process.argv))
            if app is None:
                continue
            if _in_unit_cgroup(self._cgroup_reader(process.pid), app.unit) is False:
                orphan_wrappers[process.pid] = (process, app)

        orphans: list[OrphanProcess] = []
        for process, app in orphan_wrappers.values():
            orphans.append(self._orphan(process, "ags-run", app))
        for process in processes:
            if not _is_gjs(process.argv):
                continue
            ancestor = self._wrapper_ancestor(process, by_pid)
            if ancestor is not None and ancestor.pid in orphan_wrappers:
                app = orphan_wrappers[ancestor.pid][1]
                orphans.append(self._orphan(process, "gjs", app))
        return orphans

    def _orphan(
        self, process: ProcessInfo, kind: Literal["ags-run", "gjs"], app: AlwaysOnApp
    ) -> OrphanProcess:
        return OrphanProcess(
            pid=process.pid,
            kind=kind,
            app=app.app,
            unit=app.unit,
            instance=app.instance,
            config_dir=app.config_dir,
            argv=process.argv,
        )

    def _match_app(self, config_dir: Path | None) -> AlwaysOnApp | None:
        for app in self._apps:
            if config_dir is None:
                # A bare ``ags run`` is the bar's historical launch shape.
                if app.config_dir == "ags":
                    return app
                continue
            if self._install_spine is not None:
                expected = self._install_spine / "config" / app.config_dir
                try:
                    if config_dir.resolve() == expected.resolve():
                        return app
                except OSError:
                    pass
            if config_dir.name == app.config_dir:
                return app
        return None

    @staticmethod
    def _wrapper_ancestor(
        process: ProcessInfo, by_pid: dict[int, ProcessInfo]
    ) -> ProcessInfo | None:
        seen: set[int] = set()
        current = process
        for _ in range(16):
            parent = by_pid.get(current.ppid)
            if parent is None or parent.pid in seen:
                return None
            seen.add(parent.pid)
            if _is_ags_run(parent.argv):
                return parent
            current = parent
        return None


class AgsSupervisionRepair:
    """Apply only the RFC §4.2 graceful repair actions."""

    def __init__(
        self,
        *,
        apps: Sequence[AlwaysOnApp] = ALWAYS_ON_APPS,
        command_runner: CommandRunner | None = None,
        systemctl: str = "systemctl",
        ags: str = "ags",
    ) -> None:
        self._apps = tuple(apps)
        self._run = command_runner or _run_command
        self._systemctl = systemctl
        self._ags = ags

    def repair(self, report: SupervisionReport) -> tuple[SupervisionAction, ...]:
        """Restart unhealthy units and gracefully quit displaced instances."""
        if not report.available:
            return ()
        by_unit = {unit.unit: unit for unit in report.units}
        quit_instances: set[str] = set()
        restart_units: set[str] = set()
        for unit in report.units:
            if unit.healthy:
                continue
            restart_units.add(unit.unit)
            if not unit.active and unit.bus_owner_pid is not None:
                quit_instances.add(unit.instance)
            elif unit.active and unit.bus_owner_in_unit is False:
                quit_instances.add(unit.instance)
        for orphan in report.orphans:
            quit_instances.add(orphan.instance)
            restart_units.add(orphan.unit)

        actions: list[SupervisionAction] = []
        for app in self._apps:
            if app.instance in quit_instances:
                actions.append(
                    self._action(
                        "quit",
                        app,
                        app.instance,
                        [self._ags, "quit", "-i", app.instance],
                    )
                )
            unit_status = by_unit.get(app.unit)
            if app.unit in restart_units or (unit_status is not None and not unit_status.healthy):
                actions.append(
                    self._action(
                        "restart",
                        app,
                        app.unit,
                        [self._systemctl, "--user", "restart", app.unit],
                    )
                )
        return tuple(actions)

    def _action(
        self,
        kind: Literal["quit", "restart"],
        app: AlwaysOnApp,
        target: str,
        argv: Sequence[str],
    ) -> SupervisionAction:
        result = self._call(argv)
        return SupervisionAction(
            kind=kind,
            app=app.app,
            target=target,
            argv=tuple(argv),
            returncode=result.returncode,
        )

    def _call(self, argv: Sequence[str]) -> CommandResult:
        try:
            return self._run(argv)
        except (FileNotFoundError, OSError) as exc:
            return CommandResult(returncode=127, stderr=str(exc))
