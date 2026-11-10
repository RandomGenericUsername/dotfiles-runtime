"""Read-only Hyprland socket2 event source (H-3.4, no polling).

Hyprland publishes an event stream on
``$XDG_RUNTIME_DIR/hypr/<signature>/.socket2.sock``. The instance signature is
taken from ``HYPRLAND_INSTANCE_SIGNATURE``; without it, exactly one instance
from ``hyprctl -j instances`` is accepted. Zero or multiple instances are
treated as unavailable rather than guessed.

The source is deliberately best-effort: a missing socket, a closed stream or a
malformed line is degraded health, never a daemon crash. It only reads event
bytes; it never asks the compositor for host values. The convergence path
alone reads those inside ``converge.lock``.
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from runtime.adapters.hyprland_reloader import _resolve_hyprctl
from runtime.domain.host_events import HostEvent, HostEventKind
from runtime.domain.watch import WatchStatus
from runtime.ports.host_event_source import IHostEventSource

logger = logging.getLogger(__name__)

SOCKET2_FAILURE_ROOT = "hyprland:socket2"
_EVENT_NAME = HostEventKind.CONFIG_RELOADED.value

_SUBPROCESS_ERRORS: tuple[type[BaseException], ...] = (
    FileNotFoundError,
    PermissionError,
    subprocess.TimeoutExpired,
    OSError,
    ValueError,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


class HyprlandConfigSocketSource(IHostEventSource):
    """A blocking ``configreloaded`` reader over Hyprland's socket2."""

    def __init__(
        self,
        *,
        socket_path: Path | None = None,
        hyprctl_path: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 10,
        runner: Runner | None = None,
    ) -> None:
        self._explicit_socket = socket_path
        self._hyprctl_path = _resolve_hyprctl(hyprctl_path)
        self._env = dict(env) if env is not None else None
        self._timeout = timeout
        self._runner: Runner = runner or subprocess.run
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._reason: str | None = None
        self._started = False

    # -- discovery -----------------------------------------------------

    def _run(self, args: list[str], env: Mapping[str, str]) -> Any | None:
        if self._hyprctl_path is None:
            return None
        try:
            result = self._runner(
                [str(self._hyprctl_path), *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=dict(env),
                check=False,
            )
        except _SUBPROCESS_ERRORS as exc:
            logger.warning("host events: hyprctl %s failed: %s", " ".join(args), exc)
            return None
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            logger.warning("host events: hyprctl %s returned invalid JSON: %s", " ".join(args), exc)
            return None

    def _instance_signature(self, env: dict[str, str]) -> str | None:
        signature = env.get("HYPRLAND_INSTANCE_SIGNATURE")
        if signature:
            return signature
        payload = self._run(["-j", "instances"], env)
        if not isinstance(payload, list) or len(payload) != 1:
            logger.warning(
                "host events: expected exactly one Hyprland instance, got %s",
                len(payload) if isinstance(payload, list) else type(payload).__name__,
            )
            return None
        entry = payload[0]
        instance = entry.get("instance") if isinstance(entry, dict) else None
        if not isinstance(instance, str) or not instance:
            logger.warning("host events: Hyprland instance entry has no instance name")
            return None
        return instance

    def _resolve_socket_path(self) -> tuple[Path | None, str | None]:
        if self._explicit_socket is not None:
            return self._explicit_socket, None
        base_env = dict(os.environ if self._env is None else self._env)
        signature = self._instance_signature(base_env)
        if signature is None:
            return None, "no single Hyprland instance signature"
        runtime_dir = base_env.get("XDG_RUNTIME_DIR")
        if not runtime_dir:
            return None, "XDG_RUNTIME_DIR is unset"
        return Path(runtime_dir) / "hypr" / signature / ".socket2.sock", None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
        path, reason = self._resolve_socket_path()
        if path is None:
            self._degrade(reason or "socket2 unavailable")
            return
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._timeout)
            sock.connect(str(path))
            sock.setblocking(False)
        except OSError as exc:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self._degrade(f"cannot connect to {path}: {exc}")
            return
        with self._lock:
            self._sock = sock
            self._reason = None
            self._buffer = b""
        logger.info("host events: subscribed to %s", path)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            sock = self._sock
            self._sock = None
            self._buffer = b""
            self._started = False
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _degrade(self, reason: str) -> None:
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        with self._lock:
            self._reason = reason
        logger.warning("host events: %s; watch health degraded", reason)

    # -- reading -------------------------------------------------------

    @staticmethod
    def _parse_line(line: bytes) -> bool:
        """True when one protocol line is a ``configreloaded`` event."""
        try:
            text = line.decode("utf-8", errors="replace").strip()
        except Exception:  # pragma: no cover - decode(errors=replace) cannot raise
            return False
        name, _sep, _payload = text.partition(">>")
        return name.strip() == _EVENT_NAME

    def _take_event(self) -> HostEvent | None:
        """Drain complete buffered lines; return at most one coalesced event."""
        with self._lock:
            if b"\n" not in self._buffer:
                return None
            # Treat the whole recv batch as one burst: the partial tail stays
            # buffered, every complete line is consumed together so a
            # multi-line reload notification fires once.
            lines = self._buffer.split(b"\n")
            self._buffer = lines.pop()
        if any(self._parse_line(line) for line in lines):
            return HostEvent(kind=HostEventKind.CONFIG_RELOADED, raw="configreloaded")
        return None

    def read_event(self, timeout: float | None = None) -> HostEvent | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                sock = self._sock
            if sock is None:
                # No socket: wait on shutdown for the requested window. This is
                # not a poll (no state is read) and never busy-loops.
                if timeout is None:
                    self._stop.wait()
                else:
                    self._stop.wait(timeout)
                return None

            event = self._take_event()
            if event is not None:
                return event

            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
            try:
                ready, _writable, _exceptional = select.select([sock], [], [], remaining)
            except (OSError, ValueError) as exc:
                self._degrade(f"socket select failed: {exc}")
                return None
            if not ready:
                return None
            try:
                chunk = sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                self._degrade(f"socket read failed: {exc}")
                return None
            if not chunk:
                self._degrade("socket closed by Hyprland")
                return None
            with self._lock:
                self._buffer += chunk

    def status(self) -> WatchStatus:
        with self._lock:
            connected = self._sock is not None
            reason = self._reason
        if connected:
            return WatchStatus(registered=1)
        return WatchStatus(
            registered=0,
            failed=(SOCKET2_FAILURE_ROOT,),
            last_error=reason or "socket2 unavailable",
        )
