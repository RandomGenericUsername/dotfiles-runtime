"""Unit tests for the btop reload adapter (SIGUSR2).

Mirrors the kitty reloader's four R5 outcomes and its injectable seams, so no
real process is signalled and no live btop is touched:

- no btop process    => vacuously True (nothing signalled);
- one/many btop      => each pid receives SIGUSR2 exactly once => True;
- a failed signal    => False (surfaced; appears in reload_failures);
- default pid source => ``pgrep -x btop`` parsing, no shell, no pty.

The signal number is asserted explicitly: btop registers SIGUSR1 as a bare
Input::poll no-op (btop.cpp:314-315) and SIGWINCH only forces a resize redraw.
SIGUSR2 is the one that re-runs init_config + Theme::updateThemes() +
Theme::setTheme() (btop.cpp:1053, :1121-1130), so sending anything else would
make this adapter silently do nothing.
"""

from __future__ import annotations

import signal
import subprocess

from runtime.adapters.btop_reloader import BtopReloader, _default_pid_source
from runtime.ports.desktop_reloader import IDesktopReloader


class _Recorder:
    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self._fail_on = fail_on or set()

    def __call__(self, pid: int, sig: int) -> None:
        self.calls.append((pid, sig))
        if pid in self._fail_on:
            raise PermissionError(f"cannot signal {pid}")


class TestBtopReloaderVacuous:
    def test_no_btop_is_vacuous_true(self) -> None:
        signaller = _Recorder()
        reloader = BtopReloader(pid_source=lambda: [], signaller=signaller)

        assert reloader.reload() is True
        assert signaller.calls == []


class TestBtopReloaderSignals:
    def test_single_btop_gets_exactly_one_sigusr2(self) -> None:
        signaller = _Recorder()
        reloader = BtopReloader(pid_source=lambda: [4242], signaller=signaller)

        assert reloader.reload() is True
        assert signaller.calls == [(4242, signal.SIGUSR2)]

    def test_many_btop_each_signalled_once(self) -> None:
        signaller = _Recorder()
        reloader = BtopReloader(pid_source=lambda: [11, 22, 33], signaller=signaller)

        assert reloader.reload() is True
        assert signaller.calls == [
            (11, signal.SIGUSR2),
            (22, signal.SIGUSR2),
            (33, signal.SIGUSR2),
        ]

    def test_never_sends_sigusr1_or_sigwinch(self) -> None:
        """SIGUSR1 is a no-op in btop; sending it would be a silent no-reload."""
        signaller = _Recorder()
        BtopReloader(pid_source=lambda: [7], signaller=signaller).reload()

        assert signaller.calls
        for _pid, sig in signaller.calls:
            assert sig == signal.SIGUSR2
            assert sig not in (signal.SIGUSR1, signal.SIGWINCH)


class TestBtopReloaderFailure:
    def test_failed_signal_is_surfaced_false(self) -> None:
        signaller = _Recorder(fail_on={99})
        reloader = BtopReloader(pid_source=lambda: [99], signaller=signaller)

        assert reloader.reload() is False

    def test_partial_failure_is_still_false(self) -> None:
        """One unreachable pid fails the whole reload, as for kitty."""
        signaller = _Recorder(fail_on={22})
        reloader = BtopReloader(pid_source=lambda: [11, 22, 33], signaller=signaller)

        assert reloader.reload() is False
        assert [pid for pid, _ in signaller.calls] == [11, 22, 33]


class TestBtopReloaderPort:
    def test_implements_the_reloader_port(self) -> None:
        assert isinstance(BtopReloader(pid_source=lambda: []), IDesktopReloader)


class TestDefaultPidSource:
    def test_parses_pgrep_stdout(self, monkeypatch) -> None:
        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ARG001
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "101\n202\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert _default_pid_source() == [101, 202]
        assert captured == [["pgrep", "-x", "btop"]]

    def test_no_matches_is_empty(self, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ARG001
            return subprocess.CompletedProcess(cmd, 1, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert _default_pid_source() == []

    def test_skips_junk_and_nonpositive_tokens(self, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ARG001
            return subprocess.CompletedProcess(cmd, 0, "abc\n0\n-5\n77\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert _default_pid_source() == [77]

    def test_pgrep_missing_degrades_to_empty_not_failure(self, monkeypatch) -> None:
        """Enumeration is best-effort: a missing pgrep must not surface."""

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ARG001
            raise FileNotFoundError("pgrep")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert _default_pid_source() == []