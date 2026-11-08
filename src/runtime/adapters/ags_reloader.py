"""AGS restart reload adapter — restarts AGS consumers after a swap (AD-17, FR-6, R5).

Implements ``IDesktopReloader``: after the swap repoints
``current/colors.gtk.css`` (through the ``~/.config/ags`` spine symlink to
``colors.css``, applied at runtime via ``app.apply_css``), AGS has NO native
hot-reload (verified in the AGS source, ``cli/cmd/run.go:145`` ``// TODO: watch
and restart`` [ARCHITECTURE-SPINE.md:236]), so a restart is the only reload
channel. This includes the standalone AGS tools (``hypr-pano``, ``capture``,
…) that also read the generated palette: each running instance is restarted
with the argv it was launched with, so the palette change propagates exactly
like it does for the bar.

The bar (default instance ``ags``, launched as a bare ``ags run``) is restarted
unconditionally, mirroring the original Story 2.4 behavior. The standalone tool
instances are discovered from ``/proc`` (their ``ags run -d <dir>`` argv), and
only running ones are restarted; an instance whose config dir is in
``skip_config_dirs`` is left alone (the editor ``ags-icme`` may hold unsaved
state).

Restart procedure (AD-28, BUG-006)
----------------------------------

AGS 3.1.0 bundles every app to the same per-user ``$XDG_RUNTIME_DIR/ags.js``,
and its ``Gtk.Application`` remote path exits 0 when it delegates to a live
instance. A restart therefore has to be serialized and confirmed:

1. Acquire the shared per-user AGS launch lock
   (``$XDG_RUNTIME_DIR/ags-launch.lock``). Every ``ags run`` caller must hold
   the same lock so the shared bundle cannot be overwritten between bundling
   and ``gjs`` load.
2. Quit the intended instance (``ags quit -i <instance>``; a non-zero exit
   deterministically means no live instance) and wait up to 5 s for its D-Bus
   name (and process, when the old wrapper pid is known) to release. If the
   name is still owned, fail without launching — a launch would only delegate
   to the surviving old instance.
3. Launch a detached ``ags run`` (``Popen`` + ``start_new_session=True`` —
   never a blocking ``run()``).
4. Verify the intended name appears. A clean exit 0 is *delegation* only when
   the intended name is owned afterwards; exit 0 without the name is a real
   failure, not a success. A non-zero exit is always a failure.

Unit-managed apps (REQ-018) skip this sequence: the blocking
``systemctl --user restart <unit>`` job owns stop/start, and a fresh
``NameOwnerChanged`` for ``io.Astal.<instance>`` confirms the new owner. The
restart argv never carries ``--wait``: for a long-running unit systemd defines
that as "wait until the started unit stops again", which can never complete
for a healthy bar. The whole restart (job + owner event) is bounded by one
configurable deadline.

Missing ``ags`` in PATH is a surfaced failure, not a skip (spec-literal R5;
same decision as the Hyprland adapter).

Binary resolution imports ``_resolve_via_which`` from ``hyprland_reloader``
(adapters→adapters import — layering-green) for the bare-name ``PATH`` lookup;
the separator/executable-file branch structure mirrors
``hyprland_reloader._resolve_hyprctl``.
"""

from __future__ import annotations

import fcntl
import logging
import math
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from runtime.adapters.ags_systemd_units import (
    DEFAULT_RESTART_TIMEOUT,
    AgsSystemdUnit,
    SystemdCommandResult,
    SystemdCommandRunner,
    discover_ags_systemd_units,
    unit_for_config_dir,
)
from runtime.adapters.hyprland_reloader import _resolve_via_which
from runtime.ports.desktop_reloader import IDesktopReloader

logger = logging.getLogger(__name__)

#: Shared per-user AGS launch lock filename (AD-28). Every ``ags run`` caller
#: (runtime restart lane, provisioning, autostart, on-demand launchers) must
#: flock ``$XDG_RUNTIME_DIR/ags-launch.lock``. ``ags request`` does not bundle
#: and is excluded.
AGS_LAUNCH_LOCK_FILENAME = "ags-launch.lock"

#: Default AGS instance name for the bar (``ags run`` with no ``-d``).
BAR_INSTANCE_NAME = "ags"

#: Bounded quit-release wait: 20 × 0.25 s = 5 s (AD-28).
_QUIT_WAIT_POLLS = 20
_QUIT_WAIT_POLL_INTERVAL = 0.25

#: Bounded post-launch name-registration wait: 20 × 0.25 s = 5 s.
_STARTUP_WAIT_POLLS = 20
_STARTUP_WAIT_POLL_INTERVAL = 0.25

#: D-Bus readiness bound after a unit start job. The unit wrapper registers
#: with ``--register-timeout 10``; 30 s is 3× that window, so a slow
#: registration is not misreported as a dead start. The unit restart path
#: caps this by whatever remains of its own :data:`DEFAULT_RESTART_TIMEOUT`.
DEFAULT_INSTANCE_READY_TIMEOUT = 30.0

#: Bounded shared launch-lock wait: 100 × 0.1 s = 10 s.
_LAUNCH_LOCK_POLLS = 100
_LAUNCH_LOCK_POLL_INTERVAL = 0.1

#: Config-dir names never restarted automatically. The icon color mapping
#: editor can hold unsaved edits; a wallpaper change must not discard them.
#: The btop theme studio is the same class: its pending theme edits live only
#: in memory until Save, so a restart would silently destroy them. It has no
#: live refresh yet; leaving it stale is the deliberate safety trade.
#: The wallpaper selector is on-demand with a self-hiding window: restarting
#: it after a set would pop the dialog back open uninvited (it starts
#: visible, ICME pattern). It refreshes its palette live from the
#: ``wallpaper.state`` domain event instead (no restart needed). The
#: notification overlay also subscribes live: restarting it during wallpaper
#: reconciliation would discard the progress card before it reports done.
DEFAULT_SKIP_CONFIG_DIRS: frozenset[str] = frozenset(
    {"ags-icme", "ags-btop-studio", "ags-wallpaper-selector", "ags-notifications"}
)

_INSTANCE_NAME_RE = re.compile(r"""instanceName\s*:\s*["']([^"']+)["']""")


class _AgsQueryError(RuntimeError):
    """The AGS instance/name oracle could not be queried."""


class _AgsLaunchLockError(RuntimeError):
    """The shared AGS launch lock could not be acquired."""


def _resolve_ags(ags_path: Path | None) -> Path | None:
    """Resolve the ``ags`` binary path (mirrors ``hyprland_reloader``)."""
    if ags_path is not None:
        candidate = str(ags_path)
        if (
            os.path.sep in candidate
            or (os.path.altsep and os.path.altsep in candidate)
            or "\\" in candidate
        ):
            if ags_path.is_file():
                try:
                    return ags_path if os.access(candidate, os.X_OK) else None
                except OSError:
                    return None
            return None
        return _resolve_via_which(candidate)
    return _resolve_via_which("ags")


def _default_launch_lock_path() -> Path:
    """Return the shared per-user AGS launch-lock path.

    Production (graphical session): ``$XDG_RUNTIME_DIR/ags-launch.lock``.
    Headless/test fallback (no ``XDG_RUNTIME_DIR``): a per-uid path under the
    system temporary directory. The lock is a kernel ``flock``: it is released
    automatically when the holder exits, so there is no stale-file cleanup.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / AGS_LAUNCH_LOCK_FILENAME
    return Path(tempfile.gettempdir()) / f"ags-{os.getuid()}" / AGS_LAUNCH_LOCK_FILENAME


def _resolve_launch_lock_path(launch_lock_path: Path | None) -> Path:
    if launch_lock_path is not None:
        return launch_lock_path.expanduser().resolve()
    return _default_launch_lock_path()


@contextmanager
def _hold_launch_lock(lock_path: Path) -> Iterator[None]:
    """Hold the shared AGS launch lock, bounded to ~10 s.

    Raises:
        _AgsLaunchLockError: the lock file cannot be opened, the wait times
            out, or flock fails for a reason other than contention.
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise _AgsLaunchLockError(f"cannot open AGS launch lock {lock_path}: {exc}") from exc

    acquired = False
    try:
        try:
            # One non-blocking attempt: a held launch lock is a busy failure,
            # never a sleep/retry loop. The kernel releases it on death.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _AgsLaunchLockError(
                f"AGS launch lock is held by another process: {lock_path}"
            ) from exc
        except OSError as exc:
            raise _AgsLaunchLockError(
                f"cannot acquire AGS launch lock {lock_path}: {exc}"
            ) from exc
        acquired = True
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


def _list_ags_instances(ags_bin: Path) -> list[str]:
    """Return the live AGS instance names (``ags list``).

    ``ags list`` prints one ``io.Astal.<instance>`` short name per line. It is
    the AGS-native ownership oracle: a name present here is owned by a live
    ``Gtk.Application`` and a second ``ags run`` would delegate to it and
    exit 0.
    """
    try:
        result = subprocess.run(
            [str(ags_bin), "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (
        FileNotFoundError,
        PermissionError,
        subprocess.TimeoutExpired,
        OSError,
        ValueError,
    ) as exc:
        raise _AgsQueryError(f"ags list failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or "no stderr"
        raise _AgsQueryError(f"ags list exited with code {result.returncode}: {detail}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _pid_alive(pid: int) -> bool:
    """Probe process liveness without signalling it (``kill(pid, 0)``)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True, slots=True)
class AgsRestartResult:
    """One deployed-app restart outcome and the exact command that ran."""

    ok: bool
    argv: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True, slots=True)
class AgsAppProcess:
    """One running AGS process (``ags run``, bare or with ``-d <dir>``)."""

    pid: int
    argv: tuple[str, ...]
    config_dir: Path
    instance: str


def _instance_name_for_dir(config_dir: Path) -> str | None:
    """Read ``instanceName`` from the app's ``app.tsx`` (None if absent)."""
    try:
        text = (config_dir / "app.tsx").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _INSTANCE_NAME_RE.search(text)
    return match.group(1) if match is not None else None


def _read_process_environ(proc_entry: Path) -> dict[str, str]:
    """Read one process environment from ``/proc/<pid>/environ``.

    Unreadable/empty files yield an empty mapping rather than an error: a
    process may exit while it is being scanned, and a missing environment is
    not a reason to drop an otherwise valid AGS process.
    """
    try:
        raw = (proc_entry / "environ").read_bytes()
    except OSError:
        return {}
    environ: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, _, value = item.partition(b"=")
        environ[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return environ


def _default_config_dir(proc_entry: Path) -> Path:
    """The effective default AGS config dir for a bare ``ags run`` process.

    AGS resolves a bare ``ags run`` from the process's own environment:
    ``$XDG_CONFIG_HOME/ags`` when set, otherwise ``$HOME/.config/ags`` and
    finally this runtime's ``~/.config/ags`` when the environment is
    unreadable. The path is only an attribution; ownership is still decided
    by resolving it against the deployed link.
    """
    environ = _read_process_environ(proc_entry)
    xdg_config_home = environ.get("XDG_CONFIG_HOME", "")
    if xdg_config_home:
        return Path(xdg_config_home) / "ags"
    home = environ.get("HOME", "")
    if home:
        return Path(home) / ".config" / "ags"
    return Path.home() / ".config" / "ags"


def _discover_ags_apps(proc_root: Path | None = None) -> list[AgsAppProcess]:
    """Enumerate running AGS apps from ``/proc``.

    An explicit ``-d/--directory`` config dir is used as given. A bare
    ``ags run`` (the bar autostart) is attributed to its effective default
    config dir (see :func:`_default_config_dir`), which lets deployed-link
    discovery resolve the bar. Attribution does not weaken the ownership
    rule: a config dir that does not resolve to the deployed link is still a
    foreign/outside process.
    """
    apps: list[AgsAppProcess] = []
    proc = proc_root if proc_root is not None else Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return apps
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = tuple(part for part in raw.decode("utf-8", "replace").split("\0") if part)
        if len(argv) < 2 or Path(argv[0]).name != "ags" or argv[1] != "run":
            continue
        config_dir: Path | None = None
        for index, arg in enumerate(argv):
            if arg in ("-d", "--directory") and index + 1 < len(argv):
                config_dir = Path(argv[index + 1])
                break
        explicit_config_dir = config_dir is not None
        if config_dir is None:
            config_dir = _default_config_dir(entry)
        instance = _instance_name_for_dir(config_dir) or (
            config_dir.name if explicit_config_dir else BAR_INSTANCE_NAME
        )
        apps.append(
            AgsAppProcess(
                pid=int(entry.name),
                argv=argv,
                config_dir=config_dir,
                instance=instance,
            )
        )
    return apps


def _matches_ags_binary(argv0: str, ags_bin: Path | None) -> bool:
    """True when ``argv0`` resolves to the adapter's ``ags`` binary.

    This keeps the bar-wrapper scan from waiting on an unrelated AGS binary
    (and keeps unit tests isolated from a developer's live bar when they pass
    a fake ``ags`` path).
    """
    if ags_bin is None:
        return Path(argv0).name == "ags"
    candidate = Path(argv0) if os.path.sep in argv0 else _resolve_via_which(argv0)
    if candidate is None:
        return False
    try:
        return candidate.resolve() == ags_bin.resolve()
    except OSError:
        return False


def _discover_bar_pids(ags_bin: Path | None = None) -> list[int]:
    """Enumerate bare ``ags run`` wrapper pids from ``/proc``.

    The bar has no ``-d`` config dir, so it is absent from
    :func:`_discover_ags_apps`. A bare wrapper that is still alive after
    ``ags quit`` may be cold-bundling or tearing down; waiting on its pid
    prevents launching a duplicate before it registers or exits.
    """
    pids: list[int] = []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return pids
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = tuple(part for part in raw.decode("utf-8", "replace").split("\0") if part)
        if len(argv) < 2 or argv[1] != "run":
            continue
        if any(arg in ("-d", "--directory") for arg in argv):
            continue
        if not _matches_ags_binary(argv[0], ags_bin):
            continue
        pids.append(int(entry.name))
    return pids


class AgsReloader(IDesktopReloader):
    """Restarts the bar and the standalone AGS consumers on a palette swap.

    Args:
        ags_path: explicit path to the ``ags`` binary. When ``None``,
            resolved via ``shutil.which("ags")``; if not found, the
            adapter stores ``None`` and ``reload()`` returns ``False``
            without spawning a subprocess (surfaced failure, not a skip).
        app_lister: injectable discovery of running standalone apps
            (defaults to a ``/proc`` scan) — tests inject a fake.
        instance_lister: injectable live-instance oracle (defaults to
            ``ags list``) — tests inject a scripted fake. It must return the
            AGS short instance names (``ags``, ``capture``, …).
        bar_pid_lister: injectable discovery of bare ``ags run`` bar wrapper
            pids (defaults to a ``/proc`` scan for this adapter's ``ags``
            binary) — tests inject a fake. A known old wrapper pid must exit
            before the replacement launches.
        process_alive: injectable liveness probe used to confirm a known old
            wrapper pid has exited (defaults to ``os.kill(pid, 0)``).
        launch_lock_path: shared per-user launch-lock path (defaults to
            ``$XDG_RUNTIME_DIR/ags-launch.lock``; tests pass a tmp path).
        skip_config_dirs: config-dir names never restarted automatically.
        systemd_unit_lister: injectable discovery of ``dotfiles-ags-*.service``
            units (defaults to ``systemctl --user list-unit-files`` + one
            ``ExecStart`` read per unit) — tests inject a fake.
        systemctl_runner: injectable systemctl command seam (defaults to a
            bounded ``subprocess.run``) used for unit discovery and restart.
            An injected runner owns its own bound; the default runner applies
            ``systemd_restart_timeout`` to the restart command.
        systemctl: systemctl executable name/path.
        systemd_restart_timeout: whole-unit-restart deadline in seconds
            (default :data:`DEFAULT_RESTART_TIMEOUT`, 90 s). It bounds the
            blocking ``systemctl restart`` job and the remaining time is what
            the D-Bus readiness wait may use.
        instance_ready_timeout: D-Bus owner-event bound in seconds (default
            :data:`DEFAULT_INSTANCE_READY_TIMEOUT`, 30 s = 3× the unit's
            ``--register-timeout 10``).
        clock: monotonic clock seam used to enforce the restart deadline.
    """

    def __init__(
        self,
        ags_path: Path | None = None,
        *,
        app_lister: Callable[[], Sequence[AgsAppProcess]] = _discover_ags_apps,
        instance_lister: Callable[[], Sequence[str]] | None = None,
        bar_pid_lister: Callable[[], Sequence[int]] | None = None,
        process_alive: Callable[[int], bool] = _pid_alive,
        launch_lock_path: Path | None = None,
        skip_config_dirs: frozenset[str] = DEFAULT_SKIP_CONFIG_DIRS,
        systemd_unit_lister: Callable[[], Sequence[AgsSystemdUnit]] | None = None,
        systemctl_runner: SystemdCommandRunner | None = None,
        systemctl: str = "systemctl",
        systemd_restart_timeout: float = DEFAULT_RESTART_TIMEOUT,
        instance_ready_timeout: float = DEFAULT_INSTANCE_READY_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        name_watcher: object | None = None,
    ) -> None:
        if systemd_restart_timeout <= 0:
            raise ValueError("systemd_restart_timeout must be positive")
        if instance_ready_timeout <= 0:
            raise ValueError("instance_ready_timeout must be positive")
        self._ags_path: Path | None = _resolve_ags(ags_path)
        self._app_lister = app_lister
        self._instance_lister = instance_lister
        self._bar_pid_lister = bar_pid_lister or self._default_bar_pid_lister
        self._process_alive = process_alive
        self._launch_lock_path = _resolve_launch_lock_path(launch_lock_path)
        self._skip_config_dirs = skip_config_dirs
        self._systemd_unit_lister = systemd_unit_lister
        self._systemctl_runner = systemctl_runner or self._default_systemctl_runner
        self._systemctl = systemctl
        self._systemd_restart_timeout = float(systemd_restart_timeout)
        self._instance_ready_timeout = float(instance_ready_timeout)
        self._restart_runner = systemctl_runner or self._default_restart_runner
        self._clock = clock
        #: Production uses the real D-Bus ``NameOwnerChanged`` watcher. A
        #: caller that injects an ``instance_lister`` is a headless/test
        #: harness with no guarantee of a bus, so the lister remains the
        #: deterministic oracle for that environment.
        if name_watcher is not None:
            self._name_watcher = name_watcher
        elif instance_lister is not None:
            self._name_watcher = None
        else:
            from runtime.adapters.dbus_name_watcher import DbusNameWatcher

            self._name_watcher = DbusNameWatcher(timeout=self._instance_ready_timeout)

    def _default_bar_pid_lister(self) -> Sequence[int]:
        return _discover_bar_pids(self._ags_path)

    @staticmethod
    def _default_systemctl_runner(argv: Sequence[str]) -> SystemdCommandResult:
        from runtime.adapters.ags_systemd_units import run_systemctl

        return run_systemctl(argv)

    def _default_restart_runner(self, argv: Sequence[str]) -> SystemdCommandResult:
        from runtime.adapters.ags_systemd_units import run_systemctl

        return run_systemctl(argv, timeout=self._systemd_restart_timeout)

    def _systemd_units(self) -> tuple[AgsSystemdUnit, ...]:
        """Discover the provisioned AGS units (never fails the restart lane)."""
        try:
            if self._systemd_unit_lister is not None:
                return tuple(self._systemd_unit_lister())
            return discover_ags_systemd_units(
                systemctl=self._systemctl,
                command_runner=self._systemctl_runner,
            )
        except Exception as exc:  # adapter boundary: no units means non-unit path
            logger.warning("AGS systemd discovery failed: %s", exc)
            return ()

    def restart_deployed_app(
        self,
        *,
        instance: str,
        config_dir: Path,
        argv: Sequence[str] | None = None,
        old_pids: Sequence[int] = (),
    ) -> AgsRestartResult:
        """Restart one configured app discovered from its deployed config link.

        Used by the manifest-driven restart lane (AD-28/AD-30). A unit whose
        ``ExecStart`` targets this configDir is restarted only through
        ``systemctl --user restart <unit>`` so systemd owns quit/start; that
        path never calls ``ags quit``/``ags run`` and never falls back to a raw
        launch. A non-unit app keeps the serialized quit + release-wait +
        launch procedure. The app is included even when down: the quit step is
        harmless and the confirmed-release wait succeeds immediately.
        """
        unit = unit_for_config_dir(self._systemd_units(), config_dir)
        if unit is not None:
            return self._restart_unit(unit, instance=instance)
        if self._ags_path is None:
            logger.warning("ags not found in PATH; AGS restart skipped for %s", instance)
            return AgsRestartResult(
                ok=False,
                argv=tuple(argv or ()),
                error="ags binary not found",
            )
        run_argv = (
            list(argv) if argv is not None else [str(self._ags_path), "run", "-d", str(config_dir)]
        )
        ok = self._restart(
            self._ags_path,
            run_argv,
            instance=instance,
            old_pids=old_pids,
        )
        return AgsRestartResult(
            ok=ok,
            argv=tuple(run_argv),
            error="" if ok else "raw restart did not confirm the intended instance",
        )

    def _restart_unit(self, unit: AgsSystemdUnit, *, instance: str) -> AgsRestartResult:
        """Restart one unit through systemd and verify the instance returns.

        ``systemctl --user restart`` blocks until its restart job finishes
        (the default; ``--no-block`` is what skips it). ``--wait`` must NOT be
        used here: for a long-running unit it means "wait until the started
        unit terminates again", so a healthy bar can never satisfy it
        (systemd 262 ``man systemctl``). After the job returns, readiness is
        the D-Bus owner event for ``io.Astal.<instance>``, capped by whatever
        remains of the single restart deadline.

        Deliberately does not take the AGS launch lock: the unit's
        ``ags-run-locked`` ExecStart owns that lock, and holding it here would
        deadlock the unit's own start. Serialization comes from the refresh
        operation lock and the one-unit-at-a-time restart lane.
        """
        argv = (self._systemctl, "--user", "restart", unit.unit)
        started = self._clock()
        try:
            result = self._restart_runner(argv)
        except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
            error = f"systemctl --user restart {unit.unit} failed: {exc}"
            logger.warning("AGS restart failed for %s: %s", instance, error)
            return AgsRestartResult(ok=False, argv=argv, error=error)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "no stderr"
            error = (
                f"systemctl --user restart {unit.unit} failed with code "
                f"{result.returncode}: {detail}"
            )
            logger.warning("AGS restart failed for %s: %s", instance, error)
            return AgsRestartResult(ok=False, argv=argv, error=error)
        elapsed = self._clock() - started
        remaining = self._systemd_restart_timeout - elapsed
        if not self._wait_for_instance(
            instance,
            label=instance,
            timeout=max(0.0, remaining),
        ):
            error = (
                f"systemctl --user restart {unit.unit} returned 0 but instance "
                f"{instance!r} did not return within "
                f"{self._systemd_restart_timeout:g} s"
            )
            logger.warning("AGS restart failed for %s: %s", instance, error)
            return AgsRestartResult(ok=False, argv=argv, error=error)
        logger.info(
            "AGS restart for %s: systemd restarted %s and the instance is registered",
            instance,
            unit.unit,
        )
        return AgsRestartResult(ok=True, argv=argv)

    def _wait_for_instance(
        self,
        name: str,
        *,
        label: str,
        timeout: float | None = None,
    ) -> bool:
        """Wait for the instance name after a systemd-owned restart.

        The production path blocks on ``NameOwnerChanged``; the ``ags list``
        polling branch is only the headless/test fallback when no bus watcher
        was injected.
        """
        watcher = self._name_watcher
        if watcher is not None:
            try:
                if timeout is None:
                    return bool(watcher.wait_for_owner(name))  # type: ignore[attr-defined]
                try:
                    return bool(
                        watcher.wait_for_owner(name, timeout=timeout)  # type: ignore[attr-defined]
                    )
                except TypeError:
                    # An injected watcher with the older one-argument shape.
                    return bool(watcher.wait_for_owner(name))  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning(
                    "AGS restart failed for %s: name-owner event wait failed: %s",
                    label,
                    exc,
                )
                return False
        wait = (
            timeout
            if timeout is not None
            else _STARTUP_WAIT_POLLS * _STARTUP_WAIT_POLL_INTERVAL
        )
        poll_count = max(1, math.ceil(wait / _STARTUP_WAIT_POLL_INTERVAL))
        for poll_index in range(poll_count):
            try:
                if name in self._owned_instances():
                    return True
            except _AgsQueryError as exc:
                logger.warning(
                    "AGS restart failed for %s: cannot verify %s ownership: %s",
                    label,
                    name,
                    exc,
                )
                return False
            if poll_index < poll_count - 1:
                time.sleep(_STARTUP_WAIT_POLL_INTERVAL)
        return False

    def reload(self) -> bool:
        """Restart the bar and every running standalone AGS consumer.

        Returns:
            True when the bar restarted or handed off cleanly to the intended
            live instance AND every reloaded tool did too; False on a missing
            binary, an unreleased old name/process, a failed launch, or a
            launch that never owns the intended name.
        """
        if self._ags_path is None:
            logger.warning("ags not found in PATH; AGS reload skipped")
            return False

        try:
            bar_pids = tuple(self._bar_pid_lister())
        except Exception as exc:  # adapter boundary: never launch blind
            logger.warning("AGS restart failed for bar: cannot discover old wrapper pid: %s", exc)
            return False

        units = self._systemd_units()
        restarted_units: set[str] = set()
        bar_unit = unit_for_config_dir(units, BAR_INSTANCE_NAME)
        if bar_unit is not None:
            bar_ok = self._restart_unit(bar_unit, instance=BAR_INSTANCE_NAME).ok
            restarted_units.add(bar_unit.unit)
        else:
            bar_ok = self._restart(self._ags_path, [], instance=None, old_pids=bar_pids)

        tools_ok = True
        seen_instances: set[str] = set()
        for app in self._app_lister():
            if app.instance == BAR_INSTANCE_NAME:
                # The bar (bare ``ags run`` or a ``-d`` relaunch of the bar)
                # is restarted unconditionally above; restarting it again as
                # a tool would quit the process that was just launched.
                continue
            if app.config_dir.name in self._skip_config_dirs:
                logger.debug("ags: leaving %s running (skip list)", app.instance)
                continue
            if app.instance in seen_instances:
                logger.debug(
                    "ags: duplicate wrapper for %s; restarting the instance once",
                    app.instance,
                )
                continue
            seen_instances.add(app.instance)
            unit = unit_for_config_dir(units, app.config_dir)
            if unit is not None:
                if unit.unit in restarted_units:
                    logger.debug(
                        "ags: %s already restarted through %s; not restarting twice",
                        app.instance,
                        unit.unit,
                    )
                    continue
                restarted_units.add(unit.unit)
                if not self._restart_unit(unit, instance=app.instance).ok:
                    tools_ok = False
                continue
            if not self._restart(
                self._ags_path,
                list(app.argv),
                instance=app.instance,
                old_pids=(app.pid,),
            ):
                tools_ok = False
        return bar_ok and tools_ok

    def _restart(
        self,
        ags_bin: Path,
        argv: list[str],
        *,
        instance: str | None,
        old_pids: Sequence[int] = (),
    ) -> bool:
        """Quit ``instance`` (bar when ``None``), confirm release, relaunch.

        The launch lock is held across quit + release wait + launch + name
        verification, so no conforming ``ags run`` caller can race the shared
        bundle. The old name and any known old wrapper pids must be gone
        before the new process is spawned.
        """
        label = instance or "bar"
        name = instance or BAR_INSTANCE_NAME

        try:
            with _hold_launch_lock(self._launch_lock_path):
                quit_argv = [str(ags_bin), "quit"]
                if instance is not None:
                    quit_argv += ["-i", instance]
                try:
                    quit_result = subprocess.run(
                        quit_argv,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                except (
                    FileNotFoundError,
                    PermissionError,
                    subprocess.TimeoutExpired,
                    OSError,
                    ValueError,
                ) as exc:
                    logger.debug("ags quit failed; continuing with restart: %s", exc)
                else:
                    if quit_result.returncode != 0:
                        logger.debug("ags quit reports no live instance; continuing")
                    else:
                        logger.debug("ags quit delivered; teardown is asynchronous")

                try:
                    released = self._wait_for_release(name, old_pids)
                except _AgsQueryError as exc:
                    logger.warning(
                        "AGS restart failed for %s: cannot verify %s release: %s",
                        label,
                        name,
                        exc,
                    )
                    return False
                if not released:
                    logger.warning(
                        "AGS restart failed for %s: %s is still owned after the quit "
                        "wait; not launching a duplicate",
                        label,
                        name,
                    )
                    return False

                run_argv = [str(ags_bin), "run"] if not argv else argv
                try:
                    proc = subprocess.Popen(
                        run_argv,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except (
                    FileNotFoundError,
                    PermissionError,
                    subprocess.TimeoutExpired,
                    OSError,
                    ValueError,
                ) as exc:
                    logger.warning("AGS reload failed for %s: %s", label, exc)
                    return False
                return self._wait_for_startup(proc, name, label)
        except _AgsLaunchLockError as exc:
            logger.warning("AGS restart failed for %s: %s", label, exc)
            return False

    def _owned_instances(self) -> list[str]:
        """Query the live AGS instance names (injectable oracle)."""
        if self._instance_lister is not None:
            try:
                return list(self._instance_lister())
            except _AgsQueryError:
                raise
            except Exception as exc:  # adapter boundary: fail safe, never launch blind
                raise _AgsQueryError(f"instance lister failed: {exc}") from exc
        if self._ags_path is None:
            return []
        return _list_ags_instances(self._ags_path)

    def _wait_for_release(self, name: str, old_pids: Sequence[int]) -> bool:
        """Wait up to 5 s for the old name (and known wrapper pids) to go away."""
        watcher = self._name_watcher
        if watcher is not None:
            try:
                if not watcher.wait_for_release(name):  # type: ignore[attr-defined]
                    return False
            except Exception as exc:
                logger.warning(
                    "AGS restart failed for %s: name-owner release wait failed: %s",
                    name,
                    exc,
                )
                return False
            return not any(self._process_alive(pid) for pid in old_pids)
        for poll_index in range(_QUIT_WAIT_POLLS):
            if self._released(name, old_pids):
                return True
            if poll_index < _QUIT_WAIT_POLLS - 1:
                time.sleep(_QUIT_WAIT_POLL_INTERVAL)
        return self._released(name, old_pids)

    def _released(self, name: str, old_pids: Sequence[int]) -> bool:
        names = self._owned_instances()
        if name in names:
            return False
        return not any(self._process_alive(pid) for pid in old_pids)

    def _wait_for_startup(self, proc: object, name: str, label: str) -> bool:
        """Verify the intended name is owned after the launch.

        A clean exit 0 is a GApplication delegation, not a failure: it is
        accepted only when the intended name is owned afterwards. Exit 0
        without the name, a non-zero exit, and a process that never registers
        the name are surfaced failures.
        """
        poll = proc.poll  # type: ignore[attr-defined]
        watcher = self._name_watcher
        if watcher is not None:
            try:
                owned = bool(watcher.wait_for_owner(name))  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning(
                    "AGS restart failed for %s: name-owner event wait failed: %s",
                    label,
                    exc,
                )
                return False
            code = poll()
            if code is not None and code != 0:
                logger.warning(
                    "AGS restart failed for %s: process exited with code %s", label, code
                )
                return False
            if not owned:
                logger.warning(
                    "AGS restart failed for %s: %s did not register", label, name
                )
                return False
            logger.info("AGS restart for %s: %s is registered", label, name)
            return True
        for poll_index in range(_STARTUP_WAIT_POLLS):
            code = poll()
            if code is not None:
                if code == 0:
                    try:
                        owned = name in self._owned_instances()
                    except _AgsQueryError as exc:
                        logger.warning(
                            "AGS restart failed for %s: cannot verify %s ownership: %s",
                            label,
                            name,
                            exc,
                        )
                        return False
                    if owned:
                        logger.info(
                            "AGS restart for %s: run exited 0 and handed off to the "
                            "live %s instance",
                            label,
                            name,
                        )
                        return True
                    logger.warning(
                        "AGS restart failed for %s: process exited with code 0 but %s "
                        "is not owned (no live instance); restart did not happen",
                        label,
                        name,
                    )
                    return False
                logger.warning(
                    "AGS restart failed for %s: process exited with code %s", label, code
                )
                return False
            try:
                if name in self._owned_instances():
                    logger.info("AGS restart for %s: %s is registered", label, name)
                    return True
            except _AgsQueryError as exc:
                logger.warning(
                    "AGS restart failed for %s: cannot verify %s ownership: %s",
                    label,
                    name,
                    exc,
                )
                return False
            if poll_index < _STARTUP_WAIT_POLLS - 1:
                time.sleep(_STARTUP_WAIT_POLL_INTERVAL)
        logger.warning("AGS restart failed for %s: %s did not register within 5 s", label, name)
        return False
