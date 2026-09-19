"""Unit tests for P5-1-1 daemon run skeleton (name-first, serve, release).

The bus-name seam is covered by an injected fake (no live bus/systemd in
tests — AC 7); the real jeepney owner is pinned to fail fatal without a
bus. Real-signal delivery is exercised exactly once (SIGTERM → release →
exit 0) with a stop-event watchdog so a broken handler fails the test,
never hangs the runner.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from jeepney import DBusAddress, Message, new_method_return, new_signal
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.adapters.gtk4_app_subscriber import Gtk4AppSubscriber
from runtime.cli.main import app
from runtime.domain.models import (
    BusNameContentionError,
    BusUnavailableError,
)
from runtime.domain.watch import WatchEvent, WatchEventKind
from runtime.ports.bus_name_owner import BUS_NAME, IBusNameOwner
from runtime.ports.watch_source import IWatchSource

runner = CliRunner()
TS = "2026-09-10T00:00:00Z"


class _FakeOwner(IBusNameOwner):
    """Thread-safe fake: scripted acquire failure, event-driven park."""

    def __init__(
        self,
        acquire_error: Exception | None = None,
        *,
        immediate_stop: bool = False,
    ) -> None:
        self._acquire_error = acquire_error
        self.acquired = False
        self.parked = threading.Event()
        self.releases = 0
        self.waits = 0
        self._stop = threading.Event()
        if immediate_stop:
            self._stop.set()

    def acquire(self) -> None:
        if self._acquire_error is not None:
            raise self._acquire_error
        self.acquired = True

    def release(self) -> None:
        self.releases += 1
        self._stop.set()

    def wait_until_terminated(self) -> None:
        self.waits += 1
        self.parked.set()
        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))


class _NoopHostedSubscriber:
    """Bounded stand-in so CLI-invoking tests never open a real bus."""

    def serve(self, stop_event: threading.Event) -> None:
        stop_event.wait()

    def stop(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_real_gtk4_subscriber(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the daemon CLI tests hermetic (no subscriber thread, no bus)."""
    monkeypatch.setattr(cli_main, "_build_gtk4_subscriber", lambda: _NoopHostedSubscriber())


def _state_root(tmp_path: Path) -> Path:
    from runtime.cli.main import _resolve_state_root

    return _resolve_state_root()


def _save_minimal_state(state_root: Path) -> None:
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.domain.models import (
        DesktopState,
        WallpaperEntry,
    )

    JsonStateRepository(state_root=state_root).save(
        DesktopState(
            schema_version=2,
            wallpaper=WallpaperEntry(
                hash_algorithm="sha256",
                kind="wallpaper",
                content_hash="ab" * 32,
                source_path="/img/w.png",
                imported_at=TS,
            ),
            monitors={},
            palette=None,
            effects=None,
            icons=None,
            applied_at=TS,
        )
    )


class TestCommandSurface:
    def test_daemon_run_command_wired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wiring only (`_run_daemon_run` mocked): blocking is pinned by the
        real-signal test below, not here."""
        monkeypatch.setattr(cli_main, "_build_watch_source", lambda: None)
        monkeypatch.setattr(cli_main, "_run_daemon_run", lambda **_: None)
        result = runner.invoke(app, ["daemon", "run"])
        assert result.exit_code == 0

    @pytest.mark.parametrize("subcommand", ["start", "stop"])
    def test_no_start_stop_subcommands(self, subcommand: str) -> None:
        result = runner.invoke(app, ["daemon", subcommand])
        assert result.exit_code != 0

    def test_daemon_never_auto_seeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli_output.domain.enums import OutputFormat

        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: calls.append("seed"))
        cli_main.main_callback(
            ctx=SimpleNamespace(invoked_subcommand="daemon"),
            output_format=OutputFormat.PLAIN,
        )
        assert calls == []


class TestNameFirst:
    def test_contention_fails_fast_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC 2: DO_NOT_QUEUE loss → non-zero; never exit 0 pre-ownership."""
        monkeypatch.setattr(
            cli_main,
            "_build_bus_name_owner",
            lambda service=None: _FakeOwner(BusNameContentionError("held by pid 1")),
        )
        with caplog.at_level("ERROR", logger="runtime.cli.main"):
            result = runner.invoke(app, ["daemon", "run"])
        assert result.exit_code == 1
        assert "cannot own" in caplog.text

    def test_bus_absent_is_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC 3: no bus → non-zero so the supervisor retries with backoff."""
        monkeypatch.setattr(
            cli_main,
            "_build_bus_name_owner",
            lambda service=None: _FakeOwner(BusUnavailableError("no bus")),
        )
        result = runner.invoke(app, ["daemon", "run"])
        assert result.exit_code == 1

    def test_real_owner_without_bus_fails_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Production seam: no bus → BusUnavailableError (never exit 0)."""

        def _no_bus(*args: object, **kwargs: object) -> object:
            raise OSError("cannot connect: no bus")

        monkeypatch.setattr("runtime.adapters.dbus_event_bus.open_dbus_connection", _no_bus)
        from runtime.adapters.dbus_event_bus import JeepneyNameOwner

        owner = JeepneyNameOwner()
        with pytest.raises(BusUnavailableError, match="cannot connect"):
            owner.acquire()
        # Release-then-wait is a clean no-op pair (never-owned tolerance).
        owner.release()
        done = threading.Event()
        worker = threading.Thread(target=lambda: (owner.wait_until_terminated(), done.set()))
        worker.start()
        assert done.wait(timeout=5)
        worker.join(timeout=5)


class TestStartupRouting:
    def test_unseeded_idles_benign_no_mutation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """AC 3: no current.json → info + idle holding the name, exit 0."""
        owner = _FakeOwner(immediate_stop=True)
        monkeypatch.setattr(cli_main, "_build_bus_name_owner", lambda service=None: owner)
        seed_calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: seed_calls.append("seed"))
        result = runner.invoke(app, ["daemon", "run"])
        assert result.exit_code == 0
        assert seed_calls == []  # the daemon never seeds (AD-11)
        assert owner.acquired is True
        state_root = _state_root(tmp_path)
        assert not (state_root / "current.json").exists()
        assert not (state_root / ".seed.lock").exists()

    def test_seeded_idles_without_mutation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        owner = _FakeOwner(immediate_stop=True)
        monkeypatch.setattr(cli_main, "_build_bus_name_owner", lambda service=None: owner)
        state_root = _state_root(tmp_path)
        _save_minimal_state(state_root)
        before = (state_root / "current.json").read_bytes()
        result = runner.invoke(app, ["daemon", "run"])
        assert result.exit_code == 0
        assert owner.acquired is True
        assert owner.waits == 1
        assert (state_root / "current.json").read_bytes() == before
        assert not (state_root / "history.jsonl").exists()

    def test_corrupt_store_is_fatal(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Corrupt current.json fails loud (never swallowed into idle)."""
        owner = _FakeOwner(immediate_stop=True)
        monkeypatch.setattr(cli_main, "_build_bus_name_owner", lambda service=None: owner)
        state_root = _state_root(tmp_path)
        state_root.mkdir(parents=True, exist_ok=True)
        (state_root / "current.json").write_text("{corrupt", encoding="utf-8")
        result = runner.invoke(app, ["daemon", "run"])
        assert result.exit_code == 1
        assert owner.releases >= 1  # name freed even on the fatal path

    def test_unreadable_store_is_fatal_and_releases(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Item 3: OSError (not just ValueError) still releases the name."""
        owner = _FakeOwner(immediate_stop=True)
        monkeypatch.setattr(cli_main, "_build_bus_name_owner", lambda service=None: owner)
        state_root = _state_root(tmp_path)
        state_root.parent.mkdir(parents=True, exist_ok=True)
        state_root.write_text("squatter", encoding="utf-8")  # ENOTDIR on read
        result = runner.invoke(app, ["daemon", "run"])
        assert result.exit_code == 1
        assert owner.releases >= 1


class TestSigtermRelease:
    def test_sigterm_releases_name_and_exits_zero(self, tmp_path: Path) -> None:
        """AC 2: real SIGTERM → release + exit 0, handlers restored.

        Runs the loop in the main (test) thread so signal delivery is real;
        a killer thread sends one SIGTERM once parked, with a stop-event
        watchdog so a broken handler fails loudly instead of hanging.
        """
        _ = tmp_path  # state isolation via the autouse fixture
        before_term = signal.getsignal(signal.SIGTERM)
        before_int = signal.getsignal(signal.SIGINT)
        owner = _FakeOwner()
        outcome: dict[str, object] = {}

        def _kill() -> None:
            assert owner.parked.wait(timeout=10)
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
            owner.stop()  # watchdog: never hang the runner

        killer = threading.Thread(target=_kill, daemon=True)
        killer.start()
        cli_main._run_daemon_run(owner=owner)
        killer.join(timeout=10)
        outcome["releases"] = owner.releases

        assert owner.acquired is True
        # Handler (1) + finally (1): the watchdog stop() alone would leave
        # releases == 1 via finally only, so >= 2 proves the signal path ran.
        assert outcome["releases"] >= 2
        # Handlers are restored — a leaked disposition would wedge pytest.
        assert signal.getsignal(signal.SIGTERM) == before_term
        assert signal.getsignal(signal.SIGINT) == before_int

    def test_handler_restored_after_immediate_park(self) -> None:
        before_term = signal.getsignal(signal.SIGTERM)
        before_int = signal.getsignal(signal.SIGINT)
        owner = _FakeOwner(immediate_stop=True)
        cli_main._run_daemon_run(owner=owner)
        assert signal.getsignal(signal.SIGTERM) == before_term
        assert signal.getsignal(signal.SIGINT) == before_int

    def test_seeded_sigterm_releases_and_exits_zero(self, tmp_path: Path) -> None:
        """Item 3: the real-signal path with a seeded store (no mutation)."""
        before_term = signal.getsignal(signal.SIGTERM)
        before_int = signal.getsignal(signal.SIGINT)
        state_root = _state_root(tmp_path)
        _save_minimal_state(state_root)
        before = (state_root / "current.json").read_bytes()
        owner = _FakeOwner()

        def _kill() -> None:
            assert owner.parked.wait(timeout=10)
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
            owner.stop()  # watchdog: never hang the runner

        killer = threading.Thread(target=_kill, daemon=True)
        killer.start()
        cli_main._run_daemon_run(owner=owner)
        killer.join(timeout=10)

        assert owner.acquired is True
        assert owner.releases >= 2
        assert (state_root / "current.json").read_bytes() == before
        assert not (state_root / "history.jsonl").exists()
        assert signal.getsignal(signal.SIGTERM) == before_term
        assert signal.getsignal(signal.SIGINT) == before_int

    def test_wait_raising_exits_nonzero_with_handlers_restored(self) -> None:
        """Item 3: a failing wait still restores dispositions and releases."""

        class _ExplodingOwner(_FakeOwner):
            def wait_until_terminated(self) -> None:
                self.waits += 1
                self.parked.set()
                raise RuntimeError("bus connection lost")

        before_term = signal.getsignal(signal.SIGTERM)
        before_int = signal.getsignal(signal.SIGINT)
        owner = _ExplodingOwner(immediate_stop=True)
        with pytest.raises(RuntimeError, match="bus connection lost"):
            cli_main._run_daemon_run(owner=owner)
        assert owner.releases >= 1
        assert signal.getsignal(signal.SIGTERM) == before_term
        assert signal.getsignal(signal.SIGINT) == before_int


class TestWellKnownName:
    def test_bus_name_is_versionless(self) -> None:
        assert BUS_NAME == "org.dotfiles.Events"
        assert not BUS_NAME.endswith("1")


class _BlockingWatchSource(IWatchSource):
    """Fake source: blocks until closed, so lifecycle is deterministic."""

    def __init__(self) -> None:
        self.started = False
        self.closed = threading.Event()
        self.rebuilds = 0

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed.set()

    def rebuild(self) -> None:
        self.rebuilds += 1

    def read_event(self, timeout: float | None = None) -> WatchEvent | None:
        self.closed.wait(timeout)
        return None


class TestConvergeOnStart:
    def test_converge_on_start_runs_after_acquire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC: converge-on-start is non-gating, after readiness."""
        owner = _FakeOwner(immediate_stop=True)
        monkeypatch.setattr(cli_main, "_build_bus_name_owner", lambda service=None: owner)
        calls: list[tuple[str, bool]] = []

        def _converge(trigger: object) -> None:
            calls.append((trigger.reason, trigger.full_rescan))

        cli_main._run_daemon_run(owner=owner, converge=_converge)
        assert owner.acquired is True
        assert calls == [("startup", True)]

    def test_converge_on_start_failure_is_recoverable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A converge error must not crash the unit (AD-41)."""
        owner = _FakeOwner(immediate_stop=True)
        monkeypatch.setattr(cli_main, "_build_bus_name_owner", lambda service=None: owner)

        def _converge(_trigger: object) -> None:
            raise RuntimeError("transient converge failure")

        cli_main._run_daemon_run(owner=owner, converge=_converge)
        assert owner.releases >= 1

    def test_no_converge_when_not_injected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Backward-compatible idle path used by the P5-1-1 tests."""
        owner = _FakeOwner(immediate_stop=True)
        monkeypatch.setattr(cli_main, "_build_bus_name_owner", lambda service=None: owner)
        cli_main._run_daemon_run(owner=owner)
        assert owner.acquired is True


class TestWatchLifecycle:
    def test_watch_source_started_and_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Runs on the main thread: _install_release_handlers needs the main
        # interpreter thread (signal.signal). A watchdog releases the owner
        # once the watch source has been installed.
        owner = _FakeOwner()
        monkeypatch.setattr(cli_main, "_build_bus_name_owner", lambda service=None: owner)
        source = _BlockingWatchSource()
        calls: list[str] = []

        def _converge(trigger: object) -> None:
            calls.append(trigger.reason)

            def _watchdog() -> None:
                deadline = time.monotonic() + 5
                while not source.started and time.monotonic() < deadline:
                    time.sleep(0.01)
                owner.stop()

            threading.Thread(target=_watchdog, daemon=True).start()

        cli_main._run_daemon_run(owner=owner, converge=_converge, watch_source=source)
        assert source.started
        assert source.closed.is_set()
        assert calls == ["startup"]

    def test_watch_event_kind_roundtrip(self) -> None:
        # Sanity: the coordinator consumes the same vocabulary the source emits.
        event = WatchEvent(kind=WatchEventKind.CREATED, path="/x")
        assert event.kind is WatchEventKind.CREATED


class _FakeNotifier:
    """Records sd_notify calls; watchdog ping is synchronous and deterministic."""

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.interval = 0.01
        self.ready_calls = 0
        self.watchdog_calls = 0
        self.stopping_calls = 0

    def ready(self) -> None:
        self.ready_calls += 1

    def watchdog(self) -> None:
        self.watchdog_calls += 1

    def stopping(self) -> None:
        self.stopping_calls += 1

    def run_watchdog(self, stop: threading.Event) -> None:
        self.watchdog()
        stop.wait()


class TestSystemdNotify:
    def test_ready_after_acquire_and_stopping_on_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        owner = _FakeOwner(immediate_stop=True)
        monkeypatch.setattr(cli_main, "_build_bus_name_owner", lambda service=None: owner)
        notifier = _FakeNotifier(enabled=False)
        cli_main._run_daemon_run(owner=owner, notifier=notifier)
        assert owner.acquired is True
        assert notifier.ready_calls == 1
        assert notifier.stopping_calls == 1
        assert notifier.watchdog_calls == 0  # no watchdog budget configured

    def test_watchdog_thread_pings_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        owner = _FakeOwner(immediate_stop=True)
        monkeypatch.setattr(cli_main, "_build_bus_name_owner", lambda service=None: owner)
        notifier = _FakeNotifier(enabled=True)
        cli_main._run_daemon_run(owner=owner, notifier=notifier)
        assert notifier.ready_calls == 1
        assert notifier.watchdog_calls >= 1
        assert notifier.stopping_calls == 1


class TestReactivePruneOptIn:
    """`--prune-on-reactive` / `$DOTFILES_REACTIVE_PRUNE` gates the prune leg.

    Default off: the converge is still wired observe-only/active as before,
    but the reactive prune stays off unless explicitly opted in. The flag is
    inert while observe-only (AD-35/AD-41).
    """

    def _captured(
        self,
        monkeypatch: pytest.MonkeyPatch,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        from runtime.application.watch import WatchTrigger

        captured: list[dict[str, object]] = []
        monkeypatch.setattr(cli_main, "_build_watch_source", lambda: None)
        monkeypatch.setattr(
            cli_main, "_run_reactive_converge", lambda **kwargs: captured.append(kwargs)
        )

        def _fake_daemon_run(*, converge=None, watch_source=None, **_kwargs):  # noqa: ANN001
            assert converge is not None
            converge(WatchTrigger(reason="startup", full_rescan=True))

        monkeypatch.setattr(cli_main, "_run_daemon_run", _fake_daemon_run)
        result = runner.invoke(app, args, env=env)
        assert result.exit_code == 0
        return captured

    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._captured(monkeypatch, ["daemon", "run", "--activate"])
        assert captured[0]["prune_on_reactive"] is False
        assert captured[0]["observe_only"] is False

    def test_flag_opts_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._captured(
            monkeypatch, ["daemon", "run", "--activate", "--prune-on-reactive"]
        )
        assert captured[0]["prune_on_reactive"] is True
        assert captured[0]["observe_only"] is False

    def test_env_var_opts_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._captured(
            monkeypatch,
            ["daemon", "run", "--activate"],
            env={"DOTFILES_REACTIVE_PRUNE": "1"},
        )
        assert captured[0]["prune_on_reactive"] is True

    def test_flag_without_activate_still_observe_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._captured(monkeypatch, ["daemon", "run", "--prune-on-reactive"])
        assert captured[0]["observe_only"] is True
        assert captured[0]["prune_on_reactive"] is True


class _DegradedBlockingSource(_BlockingWatchSource):
    def status(self):
        from runtime.domain.watch import WatchStatus

        return WatchStatus(registered=0, failed=("/spine/x",), last_error="ENOSPC")


class TestWatchHealthPersistence:
    def test_degraded_watch_set_is_persisted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from runtime.adapters.watch_health import WatchHealthStore

        owner = _FakeOwner()
        monkeypatch.setattr(cli_main, "_build_bus_name_owner", lambda service=None: owner)
        source = _DegradedBlockingSource()

        def _converge(_trigger: object) -> None:
            def _watchdog() -> None:
                deadline = time.monotonic() + 5
                while not source.started and time.monotonic() < deadline:
                    time.sleep(0.01)
                time.sleep(0.05)
                owner.stop()

            threading.Thread(target=_watchdog, daemon=True).start()

        cli_main._run_daemon_run(owner=owner, converge=_converge, watch_source=source)
        record = WatchHealthStore(_state_root(tmp_path)).read()
        assert record is not None
        assert record.degraded is True
        assert record.failed_roots == ("/spine/x",)
        assert record.last_error == "ENOSPC"


class _HostedSubscriber:
    """Lifecycle-recording fake host (blocks ``serve`` until stopped)."""

    def __init__(self) -> None:
        self.serving = threading.Event()
        self.returned = threading.Event()
        self.stop_calls = 0

    def serve(self, stop_event: threading.Event) -> None:
        self.serving.set()
        stop_event.wait()
        self.returned.set()

    def stop(self) -> None:
        self.stop_calls += 1


class _ScriptedConn:
    """Minimal jeepney-shaped connection for the hosted-subscriber integration."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False
        self.inbox: list[Message] = []

    def send_and_get_reply(self, message: object, timeout: float | None = None) -> object:
        from jeepney import HeaderFields

        member = message.header.fields.get(HeaderFields.member, "")  # type: ignore[attr-defined]
        self.calls.append(member)
        if member == "AddMatch":
            return new_method_return(message, None, ())  # type: ignore[arg-type]
        if member == "GetTopicState":
            encoded = {"_epoch": ("u", 1), "_seq": ("u", 0)}
            return new_method_return(message, "a{sv}", (encoded,))  # type: ignore[arg-type]
        raise AssertionError(f"unexpected bus call: {member}")

    def receive(self, *, timeout: float | None = None) -> Message:
        if self.inbox:
            return self.inbox.pop(0)
        time.sleep(0.005)
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


class _RestartSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return True


class _RecordingSubscriber(Gtk4AppSubscriber):
    """Signals when a delivered event has been fully handled."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.handled = threading.Event()

    def handle_signal(self, interface: str, member: str, body: tuple[object, ...]) -> None:
        super().handle_signal(interface, member, body)
        self.handled.set()


def _domain_signal(trigger: str) -> Message:
    from runtime.adapters import gtk4_app_subscriber as gs

    signal = new_signal(
        DBusAddress(gs.OBJECT_PATH, interface=gs.INTERFACE),
        "DomainEvent",
        "ssuua{sv}",
        (
            gs.WALLPAPER_TOPIC,
            ":1.5",
            1,
            1,
            {"state": ("s", "done"), "trigger": ("s", trigger)},
        ),
    )
    return Message.from_buffer(signal.serialise(serial=2))


class TestGtk4SubscriberHosting:
    def test_subscriber_thread_starts_and_stops_cleanly(self) -> None:
        owner = _FakeOwner()
        subscriber = _HostedSubscriber()

        def _watchdog() -> None:
            assert subscriber.serving.wait(timeout=5)
            owner.stop()

        threading.Thread(target=_watchdog, daemon=True).start()
        cli_main._run_daemon_run(owner=owner, subscriber_factory=lambda: subscriber)

        assert subscriber.serving.is_set()
        assert subscriber.returned.is_set()
        assert subscriber.stop_calls >= 1
        assert owner.releases >= 1

    @pytest.mark.parametrize(
        ("trigger", "expected"),
        [("set", 1), ("reconcile", 1), ("reactive", 1), ("regenerate", 0)],
    )
    def test_trigger_gate_over_the_hosted_consumer(self, trigger: str, expected: int) -> None:
        owner = _FakeOwner()
        conn = _ScriptedConn()
        spy = _RestartSpy()
        subscriber = _RecordingSubscriber(connect=lambda: conn, restart=spy)
        conn.inbox.append(_domain_signal(trigger))

        def _watchdog() -> None:
            assert subscriber.handled.wait(timeout=5)
            owner.stop()

        threading.Thread(target=_watchdog, daemon=True).start()
        cli_main._run_daemon_run(owner=owner, subscriber_factory=lambda: subscriber)

        assert spy.calls == expected
        assert conn.closed is True

    def test_subscriber_crash_does_not_stop_the_daemon(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        owner = _FakeOwner()

        class _Exploding:
            def serve(self, stop_event: threading.Event) -> None:
                raise RuntimeError("subscriber exploded")

            def stop(self) -> None:
                return None

        def _watchdog() -> None:
            time.sleep(0.1)
            owner.stop()

        threading.Thread(target=_watchdog, daemon=True).start()
        with caplog.at_level("ERROR", logger="runtime.cli.main"):
            cli_main._run_daemon_run(owner=owner, subscriber_factory=_Exploding)

        assert owner.releases >= 1
        assert "gtk4 subscriber thread terminated unexpectedly" in caplog.text

    def test_subscriber_factory_failure_is_contained(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        owner = _FakeOwner(immediate_stop=True)

        def _factory() -> object:
            raise RuntimeError("cannot build subscriber")

        with caplog.at_level("ERROR", logger="runtime.cli.main"):
            cli_main._run_daemon_run(owner=owner, subscriber_factory=_factory)

        assert owner.releases >= 1
        assert "could not start gtk4 subscriber" in caplog.text
