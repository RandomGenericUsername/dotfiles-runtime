"""Discover the ``dotfiles-ags-*.service`` ``systemd --user`` units.

The runtime must never raw-quit/relaunch an AGS app that systemd owns. The
unit contract is provisioning's (``dotfiles-provision`` REQ-023), so this
module reads it instead of carrying an app list:

* list the user units matching ``dotfiles-ags-*.service``;
* ask systemd for each unit's ``ExecStart``;
* parse the ``ags run -d <spine>/config/<configDir>`` target out of the
  argv (the wrapper's ``-- ags run -d …`` is included).

A unit is matched to a consumed deployed configDir by path identity first and
basename second. Nothing here mutates a unit or a process.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "AgsSystemdUnit",
    "DEFAULT_UNIT_GLOB",
    "SystemdCommandResult",
    "SystemdCommandRunner",
    "discover_ags_systemd_units",
    "parse_exec_start_config_dir",
    "run_systemctl",
    "unit_for_config_dir",
]

#: Provisioning's unit-name namespace (REQ-023). Only always-on apps get one.
DEFAULT_UNIT_GLOB = "dotfiles-ags-*.service"

_COMMAND_TIMEOUT = 10.0
_ARGV_FIELD_RE = re.compile(r"argv\[\]\s*=\s*(.*)")


@dataclass(frozen=True, slots=True)
class SystemdCommandResult:
    """The stdout/stderr/returncode shape the systemctl seam returns."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


SystemdCommandRunner = Callable[[Sequence[str]], SystemdCommandResult]


@dataclass(frozen=True, slots=True)
class AgsSystemdUnit:
    """One discovered AGS user unit and the configDir its ExecStart targets."""

    unit: str
    config_dir: str
    config_path: Path


def run_systemctl(argv: Sequence[str]) -> SystemdCommandResult:
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return SystemdCommandResult(returncode=124, stderr=f"timed out: {exc}")
    except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
        return SystemdCommandResult(returncode=127, stderr=str(exc))
    return SystemdCommandResult(
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def _exec_start_value(raw: str) -> str:
    """Return the argv portion of a ``systemctl show -p ExecStart`` value.

    Newer systemd renders the property as a struct:

    ``{ path=/usr/bin/ags-run-locked ; argv[]=… -- ags run -d /spine/config/ags
    ; ignore_errors=no ; … }``

    Older/plain values are already just the command line. Both shapes are
    accepted; the trailing property fields are dropped before tokenizing.
    """
    value = raw.strip()
    match = _ARGV_FIELD_RE.search(value)
    if match is not None:
        value = match.group(1)
    value = re.split(r"\s;\s", value, maxsplit=1)[0].strip()
    return value


def parse_exec_start_config_dir(exec_start: str) -> Path | None:
    """Extract the ``-d/--directory`` config dir from one ExecStart value.

    The search starts at the ``run`` token when present so a wrapper option
    cannot be mistaken for the app's config directory. Returns ``None`` when
    the command is not an ``ags run -d <dir>`` shape.
    """
    value = _exec_start_value(exec_start)
    if not value:
        return None
    try:
        tokens = shlex.split(value)
    except ValueError:
        return None
    if not tokens:
        return None
    start = next(
        (index + 1 for index, token in enumerate(tokens) if Path(token).name == "run"),
        0,
    )
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token in ("-d", "--directory") and index + 1 < len(tokens):
            return Path(tokens[index + 1])
        if token.startswith("--directory="):
            return Path(token.split("=", 1)[1])
        if token.startswith("-d="):
            return Path(token.split("=", 1)[1])
    return None


def discover_ags_systemd_units(
    *,
    systemctl: str = "systemctl",
    command_runner: SystemdCommandRunner | None = None,
    unit_glob: str = DEFAULT_UNIT_GLOB,
) -> tuple[AgsSystemdUnit, ...]:
    """Read the user units matching ``unit_glob`` and parse their configDir.

    A systemd-less host (no user manager) yields an empty tuple, which keeps
    the non-unit restart path available. Unreadable/odd units are skipped with
    a warning, never guessed.
    """
    run = command_runner or run_systemctl
    listed = run(
        [
            systemctl,
            "--user",
            "list-unit-files",
            unit_glob,
            "--no-legend",
            "--plain",
        ]
    )
    if listed.returncode != 0:
        logger.debug(
            "ags systemd discovery: list-unit-files failed (%s): %s",
            listed.returncode,
            (listed.stderr or listed.stdout).strip(),
        )
        return ()

    names: list[str] = []
    for line in listed.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        name = fields[0]
        if name.endswith(".service") and name.startswith(unit_glob.removesuffix("*.service")):
            names.append(name)

    units: list[AgsSystemdUnit] = []
    for name in sorted(set(names)):
        shown = run([systemctl, "--user", "show", "-p", "ExecStart", "--value", name])
        if shown.returncode != 0:
            logger.debug(
                "ags systemd discovery: cannot read %s ExecStart (%s): %s",
                name,
                shown.returncode,
                (shown.stderr or shown.stdout).strip(),
            )
            continue
        config_path = parse_exec_start_config_dir(shown.stdout)
        if config_path is None:
            logger.debug("ags systemd discovery: %s has no ags run -d target", name)
            continue
        units.append(
            AgsSystemdUnit(
                unit=name,
                config_dir=config_path.name,
                config_path=config_path,
            )
        )
    return tuple(units)


def unit_for_config_dir(
    units: Sequence[AgsSystemdUnit],
    config_dir: str | Path,
) -> AgsSystemdUnit | None:
    """Return the unit whose ExecStart target is this consumed configDir.

    Path identity wins (both sides are resolved, so a deployed link and its
    target compare equal); the basename is the fallback for callers/tests that
    only carry the configDir name.
    """
    target = Path(config_dir)
    try:
        resolved_target = target.resolve()
    except OSError:
        resolved_target = target
    for unit in units:
        try:
            if unit.config_path.resolve() == resolved_target:
                return unit
        except OSError:
            pass
        if unit.config_dir == target.name:
            return unit
    return None
