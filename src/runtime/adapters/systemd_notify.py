"""systemd ``sd_notify`` liveness signalling (AD-33/AD-41, P5 follow-up).

The daemon is ``Type=dbus``: systemd considers it started once it owns
``org.dotfiles.Events``. Ownership alone does **not** detect a process that
is wedged but still holding the name, so the unit also sets ``WatchdogSec=``
and the daemon pings ``WATCHDOG=1`` on a timer derived from ``WATCHDOG_USEC``;
if the pings stop, systemd restarts the unit (AD-33/AD-41 residual).

This is the only module that talks to the notify socket. It is deliberately
best-effort: when ``NOTIFY_SOCKET`` is absent (a plain terminal, a test, a
non-systemd supervisor) every call is a silent no-op and the daemon runs
exactly as before — never a crash, never a gated startup.

No third-party dependency: the wire format is a ``\n``-joined
``KEY=VALUE`` datagram sent to the unix socket named by ``NOTIFY_SOCKET``
(``sd_notify(3)``).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Mapping

logger = logging.getLogger(__name__)

#: Default watchdog budget when ``WATCHDOG_USEC`` is unset. The daemon pings
#: at half this, matching the unit's ``WatchdogSec=``.
DEFAULT_WATCHDOG_SECONDS = 30.0


def parse_notify_socket(value: str | None) -> str | None:
    """Normalize ``NOTIFY_SOCKET`` into an address for ``socket.connect``.

    A leading ``@`` denotes the Linux abstract namespace and maps to a
    leading NUL byte (``sd_notify(3)``). Empty/absent returns ``None``.
    """
    if not value:
        return None
    if value.startswith("@"):
        return "\0" + value[1:]
    return value


def watchdog_interval_seconds(env: Mapping[str, str] | None = None) -> float:
    """Half the configured watchdog budget, from ``WATCHDOG_USEC`` if present."""
    source = os.environ if env is None else env
    raw = source.get("WATCHDOG_USEC")
    usec: int | None = None
    if raw is not None:
        try:
            usec = int(raw)
        except ValueError:
            usec = None
    if usec is None or usec <= 0:
        return DEFAULT_WATCHDOG_SECONDS / 2
    return usec / 1_000_000 / 2


class SystemdNotifier:
    """Best-effort ``sd_notify`` sender + watchdog pinger."""

    def __init__(self, socket_path: str | None, *, interval: float | None = None) -> None:
        self._address = parse_notify_socket(socket_path)
        default_interval = watchdog_interval_seconds()
        self._interval = max(interval if interval is not None else default_interval, 0.001)

    @classmethod
    def from_env(cls) -> SystemdNotifier:
        return cls(os.environ.get("NOTIFY_SOCKET"))

    @property
    def enabled(self) -> bool:
        """True when a notify socket is configured (systemd is supervising)."""
        return self._address is not None

    @property
    def interval(self) -> float:
        """Seconds between watchdog pings (half the configured budget)."""
        return self._interval

    def notify(self, message: str) -> None:
        """Send one datagram; never raises (best-effort, logged at debug)."""
        if self._address is None:
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
                sock.connect(self._address)
                sock.sendall(message.encode("utf-8"))
        except OSError:
            logger.debug("sd_notify: send failed (ignored)", exc_info=True)

    def ready(self) -> None:
        """Tell systemd the daemon has finished starting (name owned)."""
        self.notify("READY=1")

    def watchdog(self) -> None:
        """Keep-alive ping; a missed ping triggers a systemd restart."""
        self.notify("WATCHDOG=1")

    def stopping(self) -> None:
        """Tell systemd the daemon is shutting down cleanly."""
        self.notify("STOPPING=1")

    def run_watchdog(self, stop: threading.Event) -> None:
        """Ping ``WATCHDOG=1`` until ``stop`` is set (blocking helper)."""
        while not stop.wait(self._interval):
            self.watchdog()
