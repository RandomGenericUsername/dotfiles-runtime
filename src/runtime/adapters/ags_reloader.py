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
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

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
        for poll_index in range(_LAUNCH_LOCK_POLLS):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if poll_index < _LAUNCH_LOCK_POLLS - 1:
                    time.sleep(_LAUNCH_LOCK_POLL_INTERVAL)
                continue
            except OSError as exc:
                raise _AgsLaunchLockError(
                    f"cannot acquire AGS launch lock {lock_path}: {exc}"
                ) from exc
            acquired = True
            break
        if not acquired:
            raise _AgsLaunchLockError(
                f"timed out waiting for the shared AGS launch lock {lock_path}"
            )
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
class AgsAppProcess:
    """One running standalone AGS app (``ags run -d <dir>``)."""

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


def _discover_ags_apps() -> list[AgsAppProcess]:
    """Enumerate running standalone AGS apps from ``/proc`` (bar excluded).

    The bar runs as a bare ``ags run`` with no config dir; it is restarted
    separately and never appears here.
    """
    apps: list[AgsAppProcess] = []
    proc = Path("/proc")
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
        if config_dir is None:
            continue
        instance = _instance_name_for_dir(config_dir) or config_dir.name
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
    ) -> None:
        self._ags_path: Path | None = _resolve_ags(ags_path)
        self._app_lister = app_lister
        self._instance_lister = instance_lister
        self._bar_pid_lister = bar_pid_lister or self._default_bar_pid_lister
        self._process_alive = process_alive
        self._launch_lock_path = _resolve_launch_lock_path(launch_lock_path)
        self._skip_config_dirs = skip_config_dirs

    def _default_bar_pid_lister(self) -> Sequence[int]:
        return _discover_bar_pids(self._ags_path)

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

        bar_ok = self._restart(self._ags_path, [], instance=None, old_pids=bar_pids)

        tools_ok = True
        seen_instances: set[str] = set()
        for app in self._app_lister():
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
