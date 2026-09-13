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
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.cli.main import app
from runtime.domain.models import (
    BusNameContentionError,
    BusUnavailableError,
)
from runtime.ports.bus_name_owner import BUS_NAME, IBusNameOwner

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
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))


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
        monkeypatch.setattr(cli_main, "_run_daemon_run", lambda owner=None: None)
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
