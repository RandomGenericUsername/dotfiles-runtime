"""H-3.4 socket2 ``configreloaded`` convergence and watch health.

The transport tests use a real AF_UNIX fake socket2 server, so the adapter's
protocol parsing, debounce and shutdown path are exercised without Hyprland.
No test uses ``sleep`` as a correctness mechanism: every wait is an event or a
bounded blocking read.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import runtime.cli.main as cli_main
from runtime.adapters.hyprland_event_source import HyprlandConfigSocketSource
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.application.doctor import DoctorUseCase
from runtime.application.host_watch import (
    HostEventCoordinator,
    WatchStatusAggregator,
)
from runtime.application.watch import WatchTrigger
from runtime.domain.host_events import HostEvent, HostEventKind
from runtime.domain.watch import WatchStatus
from runtime.ports.bus_name_owner import IBusNameOwner
from runtime.ports.host_event_source import IHostEventSource
from tests.unit.test_appearance_v2_assets import _TEMPLATES, _write_palette
from tests.unit.test_host_defaults import (
    _MANIFESTS,
    _commit,
    _compose,
    _snapshot,
)


class _FakeSocket2Server:
    """Minimal Hyprland socket2 stand-in: accepts one client and sends bytes."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(1)
        self._condition = threading.Condition()
        self._conn: socket.socket | None = None
        self._thread = threading.Thread(target=self._accept, daemon=True)

    def _accept(self) -> None:
        try:
            conn, _addr = self._listener.accept()
        except OSError:
            return
        with self._condition:
            self._conn = conn
            self._condition.notify_all()

    def start(self) -> None:
        self._thread.start()

    def wait_connected(self, timeout: float = 5) -> None:
        with self._condition:
            assert self._condition.wait_for(lambda: self._conn is not None, timeout=timeout), (
                "source did not connect"
            )

    def send(self, payload: bytes) -> None:
        self.wait_connected()
        assert self._conn is not None
        self._conn.sendall(payload)

    def close(self) -> None:
        try:
            self._listener.close()
        except OSError:
            pass
        with self._condition:
            conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def _source(path: Path) -> HyprlandConfigSocketSource:
    return HyprlandConfigSocketSource(socket_path=path)


def _rounding_runner(
    instances: list[dict[str, str]],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def _run(args, **_kwargs):
        if tuple(args[1:]) == ("-j", "instances"):
            return subprocess.CompletedProcess(args, 0, json.dumps(instances), "")
        return subprocess.CompletedProcess(args, 1, "", "missing")

    return _run


# ---------------------------------------------------------------- adapter


def test_configreloaded_line_is_parsed(tmp_path: Path) -> None:
    server = _FakeSocket2Server(tmp_path / ".socket2.sock")
    server.start()
    source = _source(server.path)
    try:
        source.start()
        assert source.status() == WatchStatus(registered=1)
        server.send(b"workspace>>2\nconfigreloaded>>\n")
        event = source.read_event(1)
        assert event is not None
        assert event.kind is HostEventKind.CONFIG_RELOADED
    finally:
        source.close()
        server.close()


def test_other_and_malformed_lines_do_not_trigger(tmp_path: Path) -> None:
    server = _FakeSocket2Server(tmp_path / ".socket2.sock")
    server.start()
    source = _source(server.path)
    try:
        source.start()
        server.send(b"not-a-protocol-line\nworkspace>>1\n\xff\xfe\n")
        assert source.read_event(0.2) is None
        server.send(b"configreloaded>>\n")
        event = source.read_event(1)
        assert event is not None and event.kind is HostEventKind.CONFIG_RELOADED
    finally:
        source.close()
        server.close()


def test_partial_line_is_buffered_until_newline(tmp_path: Path) -> None:
    server = _FakeSocket2Server(tmp_path / ".socket2.sock")
    server.start()
    source = _source(server.path)
    try:
        source.start()
        server.send(b"configre")
        assert source.read_event(0.1) is None
        server.send(b"loaded>>\n")
        event = source.read_event(1)
        assert event is not None and event.kind is HostEventKind.CONFIG_RELOADED
    finally:
        source.close()
        server.close()


def test_a_multi_line_batch_is_one_event(tmp_path: Path) -> None:
    server = _FakeSocket2Server(tmp_path / ".socket2.sock")
    server.start()
    source = _source(server.path)
    try:
        source.start()
        server.send(b"configreloaded>>\nconfigreloaded>>\nconfigreloaded>>\n")
        assert source.read_event(1) is not None
        # The whole recv batch was consumed: coalescing starts at the source.
        assert source.read_event(0.1) is None
    finally:
        source.close()
        server.close()


def test_missing_socket_is_degraded_not_fatal(tmp_path: Path) -> None:
    source = _source(tmp_path / "missing" / ".socket2.sock")
    source.start()  # must not raise
    status = source.status()
    assert status.degraded is True
    assert status.failed == ("hyprland:socket2",)
    assert source.read_event(0.05) is None
    source.close()
    source.close()  # idempotent


def test_zero_or_many_instances_are_unavailable(tmp_path: Path) -> None:
    for instances in ([], [{"instance": "a"}, {"instance": "b"}]):
        source = HyprlandConfigSocketSource(
            hyprctl_path=Path("/bin/true"),
            env={"XDG_RUNTIME_DIR": str(tmp_path)},
            runner=_rounding_runner(instances),
        )
        source.start()
        assert source.status().degraded is True
        assert "instance" in (source.status().last_error or "")
        source.close()


def test_single_instance_discovery_connects_the_expected_path(tmp_path: Path) -> None:
    socket_path = tmp_path / "hypr" / "sig" / ".socket2.sock"
    server = _FakeSocket2Server(socket_path)
    server.start()
    source = HyprlandConfigSocketSource(
        hyprctl_path=Path("/bin/true"),
        env={"XDG_RUNTIME_DIR": str(tmp_path)},
        runner=_rounding_runner([{"instance": "sig"}]),
    )
    try:
        source.start()
        assert source.status() == WatchStatus(registered=1)
        server.send(b"configreloaded>>\n")
        assert source.read_event(1) is not None
    finally:
        source.close()
        server.close()


def test_close_wakes_a_blocked_read(tmp_path: Path) -> None:
    server = _FakeSocket2Server(tmp_path / ".socket2.sock")
    server.start()
    source = _source(server.path)
    source.start()
    results: list[HostEvent | None] = []

    def _read() -> None:
        results.append(source.read_event(None))

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    server.wait_connected()
    source.close()
    reader.join(timeout=5)
    server.close()
    assert not reader.is_alive()
    assert results == [None]


# ------------------------------------------------------------ coordinator


class _ScriptedHostSource(IHostEventSource):
    def __init__(self, events: list[HostEvent], *, none_reads: int = 1) -> None:
        self._events = list(events)
        self._none_reads = none_reads
        self.on_none: Callable[[], None] | None = None
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def read_event(self, timeout: float | None = None) -> HostEvent | None:
        if self._events:
            return self._events.pop(0)
        if self._none_reads > 0:
            self._none_reads -= 1
            return None
        if self.on_none is not None:
            self.on_none()
        return None


def _event() -> HostEvent:
    return HostEvent(kind=HostEventKind.CONFIG_RELOADED, raw="configreloaded")


def test_burst_coalesces_to_one_configreloaded_trigger() -> None:
    source = _ScriptedHostSource([_event(), _event(), _event()])
    triggers: list[WatchTrigger] = []
    stop = threading.Event()
    source.on_none = stop.set

    HostEventCoordinator(source, triggers.append).serve_events(stop)

    assert len(triggers) == 1
    assert triggers[0].reason == "configreloaded"


def test_status_is_reported_only_on_change() -> None:
    source = _ScriptedHostSource([_event()])
    statuses: list[WatchStatus] = []
    stop = threading.Event()
    source.on_none = stop.set

    HostEventCoordinator(source, lambda _trigger: None, on_status=statuses.append).serve_events(
        stop
    )
    assert statuses == [WatchStatus()]


def test_watch_status_aggregator_marks_degraded_union() -> None:
    written: list[WatchStatus] = []
    aggregator = WatchStatusAggregator(written.append)
    aggregator.update("inotify", WatchStatus(registered=4))
    aggregator.update(
        "hyprland",
        WatchStatus(
            registered=0,
            failed=("hyprland:socket2",),
            last_error="socket2 unavailable",
        ),
    )
    assert written[-1].degraded is True
    assert written[-1].failed == ("hyprland:socket2",)
    assert written[-1].registered == 4
    assert written[-1].last_error == "socket2 unavailable"


def test_no_polling_static_guard() -> None:
    import runtime.adapters.hyprland_event_source as adapter
    import runtime.application.host_watch as coordinator

    for path in (adapter.__file__, coordinator.__file__):
        text = Path(path).read_text(encoding="utf-8")
        assert "time.sleep" not in text
        assert "os.stat" not in text
        assert ".stat(" not in text
        assert "os.walk" not in text
        assert "rglob" not in text


# ---------------------------------------------------- daemon integration


class _Owner(IBusNameOwner):
    def __init__(self) -> None:
        self.acquired = False
        self.parked = threading.Event()
        self._stop = threading.Event()

    def acquire(self) -> None:
        self.acquired = True

    def release(self) -> None:
        self._stop.set()

    def wait_until_terminated(self) -> None:
        self.parked.set()
        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))


def test_daemon_configreloaded_burst_converges_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.adapters.watch_health import WatchHealthStore
    from runtime.cli.main import _resolve_state_root, _run_daemon_run

    # Signal handlers only install on the main thread; this test parks the
    # owner in a worker so it can send a real socket2 burst while serving.
    monkeypatch.setattr(
        cli_main,
        "_install_release_handlers",
        lambda _owner: (lambda: None),
    )
    server = _FakeSocket2Server(tmp_path / ".socket2.sock")
    server.start()
    source = _source(server.path)
    owner = _Owner()
    triggers: list[WatchTrigger] = []

    def _converge(trigger: WatchTrigger) -> None:
        if trigger.reason == "configreloaded":
            triggers.append(trigger)
            owner.stop()

    thread = threading.Thread(
        target=_run_daemon_run,
        kwargs={
            "owner": owner,
            "converge": _converge,
            "host_event_source": source,
        },
        daemon=True,
    )
    thread.start()
    try:
        server.send(b"configreloaded>>\nconfigreloaded>>\n")
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        source.close()
        server.close()
    assert len(triggers) == 1
    record = WatchHealthStore(_resolve_state_root()).read()
    assert record is not None and record.degraded is False


# ---------------------------------------------- host identity / no-op


def test_delegated_host_change_updates_generation_and_doctor(tmp_path: Path) -> None:
    from runtime.adapters.appearance import AppearanceGenerator

    first, state_root, spine, skin = _compose(tmp_path, "glass", host=_snapshot(0, 1))
    generation_first, _manifest_first = _commit(state_root, first, skin)
    palette = _write_palette(state_root, "a" * 64, "dark")
    second, _hit = AppearanceGenerator(state_root).ensure(
        palette_entry=palette,
        skin_entry=skin,
        install_spine=spine,
        manifest_path=_MANIFESTS / "glass.toml",
        templates_dir=_TEMPLATES,
        host=_snapshot(8, 2),
    )
    generation_second, manifest_second = _commit(state_root, second, skin)

    assert generation_first != generation_second
    assert manifest_second["host"]["fingerprint"] == second.host_hash  # type: ignore[index]
    assert manifest_second["host"]["defaults"] == {  # type: ignore[index]
        "surface.panel.radius": 8,
        "surface.panel.border-width": 2,
    }

    by_name = {
        item.name: item
        for item in DoctorUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            state_root=state_root,
            install_spine=spine,
            host_probe=SimpleNamespace(read=lambda: _snapshot(8, 2)),
        )
        .check()
        .items
    }
    assert by_name["generation:host"].status == "ok"

    drifted = {
        item.name: item
        for item in DoctorUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            state_root=state_root,
            install_spine=spine,
            host_probe=SimpleNamespace(read=lambda: _snapshot(0, 1)),
        )
        .check()
        .items
    }
    assert drifted["generation:host"].status == "diverged"


def test_host_converge_opts_into_strict_idempotent_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_daemon_run import _save_minimal_state

    _save_minimal_state(cli_main._resolve_state_root())
    captured: dict[str, object] = {}

    class _Engine:
        def converge(self, intent):
            captured["intent"] = intent
            return SimpleNamespace(
                generation_hash="a" * 64,
                reconcile=SimpleNamespace(no_op=True),
            )

    monkeypatch.setattr(
        cli_main, "_build_converge_engine", lambda *_args, **_kwargs: _Engine()
    )
    assert cli_main._run_host_converge() is False
    intent = captured["intent"]
    assert intent.idempotent is True  # type: ignore[attr-defined]
    assert intent.trigger == "reactive"  # type: ignore[attr-defined]
    assert intent.source == "daemon"  # type: ignore[attr-defined]


def test_host_converge_observe_only_never_builds_an_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args, **_kwargs):
        raise AssertionError("engine must not be built in observe-only mode")

    monkeypatch.setattr(cli_main, "_build_converge_engine", _boom)
    assert cli_main._run_host_converge(observe_only=True) is False
