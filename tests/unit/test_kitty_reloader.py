"""Unit tests for the kitty reload adapter (Change 2, R5).

Covers the four R5 outcomes and the injectable seams so no real process is
signalled and no live kitty is touched:

- no kitty process  ⇒ vacuously True (nothing signalled);
- one/many kitty    ⇒ each pid receives SIGUSR1 exactly once ⇒ True;
- a failed signal   ⇒ False (surfaced; appears in reload_failures);
- default pid source ⇒ ``pgrep -x kitty`` parsing, no shell, no pty.
"""

from __future__ import annotations

import signal
import subprocess

import pytest

from runtime.adapters.kitty_reloader import KittyReloader, _default_pid_source
from runtime.ports.desktop_reloader import IDesktopReloader


class _Recorder:
    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self._fail_on = fail_on or set()

    def __call__(self, pid: int, sig: int) -> None:
        self.calls.append((pid, sig))
        if pid in self._fail_on:
            raise PermissionError(f"cannot signal {pid}")


class TestKittyReloaderVacuous:
    def test_no_kitty_is_vacuous_true(self) -> None:
        signaller = _Recorder()
        reloader = KittyReloader(pid_source=lambda: [], signaller=signaller)

        assert reloader.reload() is True
        assert signaller.calls == []

    def test_implements_port(self) -> None:
        reloader = KittyReloader(pid_source=lambda: [], signaller=_Recorder())
        assert isinstance(reloader, IDesktopReloader)


class TestKittyReloaderSignals:
    def test_one_kitty_receives_sigusr1(self) -> None:
        signaller = _Recorder()
        reloader = KittyReloader(pid_source=lambda: [4242], signaller=signaller)

        assert reloader.reload() is True
        assert signaller.calls == [(4242, signal.SIGUSR1)]

    def test_multiple_kitty_each_signalled_once(self) -> None:
        signaller = _Recorder()
        reloader = KittyReloader(pid_source=lambda: [11, 22, 33], signaller=signaller)

        assert reloader.reload() is True
        assert signaller.calls == [
            (11, signal.SIGUSR1),
            (22, signal.SIGUSR1),
            (33, signal.SIGUSR1),
        ]


class TestKittyReloaderFailure:
    def test_signal_oserror_is_surfaced_false(self) -> None:
        def _boom(pid: int, sig: int) -> None:
            raise OSError("operation not permitted")

        reloader = KittyReloader(pid_source=lambda: [7], signaller=_boom)
        assert reloader.reload() is False

    def test_one_failed_signal_still_signals_others_and_returns_false(self) -> None:
        signaller = _Recorder(fail_on={22})
        reloader = KittyReloader(pid_source=lambda: [11, 22, 33], signaller=signaller)

        assert reloader.reload() is False
        assert [pid for pid, _ in signaller.calls] == [11, 22, 33]


class TestDefaultPidSource:
    def test_parses_pgrep_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            assert args == ["pgrep", "-x", "kitty"]
            assert kwargs.get("shell", False) is False
            return subprocess.CompletedProcess(args, 0, "123\n456\n", "")

        monkeypatch.setattr("runtime.adapters.kitty_reloader.subprocess.run", _run)
        assert _default_pid_source() == [123, 456]

    def test_no_match_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 1, "", "")

        monkeypatch.setattr("runtime.adapters.kitty_reloader.subprocess.run", _run)
        assert _default_pid_source() == []

    def test_unavailable_pgrep_degrades_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("pgrep not found")

        monkeypatch.setattr("runtime.adapters.kitty_reloader.subprocess.run", _run)
        assert _default_pid_source() == []

    def test_non_numeric_output_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, "12 junk\n\n-3\n", "")

        monkeypatch.setattr("runtime.adapters.kitty_reloader.subprocess.run", _run)
        assert _default_pid_source() == [12]
