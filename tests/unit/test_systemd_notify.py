"""``sd_notify`` readiness + watchdog signalling (AD-33/AD-41, P5 follow-up).

Exercised over a real unix datagram socket (the actual wire format), plus the
graceful no-socket path: with no ``NOTIFY_SOCKET`` every call is a no-op.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

from runtime.adapters.systemd_notify import (
    DEFAULT_WATCHDOG_SECONDS,
    SystemdNotifier,
    parse_notify_socket,
    watchdog_interval_seconds,
)


def _server(tmp_path: Path) -> tuple[socket.socket, str]:
    path = tmp_path / "notify.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(path))
    server.settimeout(2.0)
    return server, str(path)


def test_parse_notify_socket() -> None:
    assert parse_notify_socket(None) is None
    assert parse_notify_socket("") is None
    assert parse_notify_socket("/run/user/1000/notify") == "/run/user/1000/notify"
    # Abstract namespace: leading '@' maps to a leading NUL.
    assert parse_notify_socket("@dotfiles-notify") == "\0dotfiles-notify"


def test_watchdog_interval_from_env() -> None:
    assert watchdog_interval_seconds({}) == DEFAULT_WATCHDOG_SECONDS / 2
    assert watchdog_interval_seconds({"WATCHDOG_USEC": "20000000"}) == 10.0
    assert watchdog_interval_seconds({"WATCHDOG_USEC": "not-a-number"}) == (
        DEFAULT_WATCHDOG_SECONDS / 2
    )
    assert watchdog_interval_seconds({"WATCHDOG_USEC": "0"}) == DEFAULT_WATCHDOG_SECONDS / 2


def test_messages_are_delivered_over_a_real_socket(tmp_path: Path) -> None:
    server, path = _server(tmp_path)
    try:
        notifier = SystemdNotifier(path)
        assert notifier.enabled is True
        notifier.ready()
        assert server.recv(4096) == b"READY=1"
        notifier.watchdog()
        assert server.recv(4096) == b"WATCHDOG=1"
        notifier.stopping()
        assert server.recv(4096) == b"STOPPING=1"
    finally:
        server.close()


def test_abstract_namespace_socket(tmp_path: Path) -> None:
    name = f"dotfiles-notify-test-{os.getpid()}"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind("\0" + name)
    server.settimeout(2.0)
    try:
        notifier = SystemdNotifier("@" + name)
        notifier.ready()
        assert server.recv(4096) == b"READY=1"
    finally:
        server.close()


def test_absent_socket_is_a_graceful_noop(monkeypatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notifier = SystemdNotifier.from_env()
    assert notifier.enabled is False
    # Must never raise.
    notifier.ready()
    notifier.watchdog()
    notifier.stopping()


def test_run_watchdog_pings_periodically(tmp_path: Path) -> None:
    server, path = _server(tmp_path)
    try:
        notifier = SystemdNotifier(path, interval=0.01)
        stop = threading.Event()
        thread = threading.Thread(target=notifier.run_watchdog, args=(stop,), daemon=True)
        thread.start()
        time.sleep(0.06)
        stop.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        pings = 0
        server.settimeout(0.2)
        while True:
            try:
                if server.recv(4096) == b"WATCHDOG=1":
                    pings += 1
            except TimeoutError:
                break
        assert pings >= 1
    finally:
        server.close()
