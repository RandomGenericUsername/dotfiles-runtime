"""Unit tests for the nvim reload adapter (SIGUSR1).

Mirrors the kitty/btop reloaders' four R5 outcomes and their injectable
seams, so no real process is signalled and no live nvim is touched:

- no nvim process    => vacuously True (nothing signalled);
- one/many nvim      => each pid receives SIGUSR1 exactly once => True;
- a failed signal    => False (surfaced; appears in reload_failures);
- default pid source => ``pgrep -x nvim`` parsing with a bounded timeout;
- injection          => a caller-supplied pid source/signaller is used
                        verbatim instead of the process/os.kill defaults.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

from runtime.adapters.nvim_reloader import NvimReloader, _default_pid_source
from runtime.ports.desktop_reloader import IDesktopReloader


class _Recorder:
    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self._fail_on = fail_on or set()

    def __call__(self, pid: int, sig: int) -> None:
        self.calls.append((pid, sig))
        if pid in self._fail_on:
            raise PermissionError(f"cannot signal {pid}")


class TestNvimReloaderVacuous:
    def test_no_nvim_is_vacuous_true(self) -> None:
        signaller = _Recorder()
        reloader = NvimReloader(pid_source=lambda: [], signaller=signaller)

        assert reloader.reload() is True
        assert signaller.calls == []


class TestNvimReloaderSignals:
    def test_single_nvim_gets_exactly_one_sigusr1(self) -> None:
        signaller = _Recorder()
        reloader = NvimReloader(pid_source=lambda: [4242], signaller=signaller)

        assert reloader.reload() is True
        assert signaller.calls == [(4242, signal.SIGUSR1)]

    def test_many_nvim_each_signalled_once(self) -> None:
        signaller = _Recorder()
        reloader = NvimReloader(pid_source=lambda: [11, 22, 33], signaller=signaller)

        assert reloader.reload() is True
        assert signaller.calls == [
            (11, signal.SIGUSR1),
            (22, signal.SIGUSR1),
            (33, signal.SIGUSR1),
        ]


class TestNvimReloaderFailure:
    def test_failed_signal_is_surfaced_false(self) -> None:
        signaller = _Recorder(fail_on={99})
        reloader = NvimReloader(pid_source=lambda: [99], signaller=signaller)

        assert reloader.reload() is False

    def test_partial_failure_is_still_false(self) -> None:
        """One unreachable pid fails the whole reload, as for kitty/btop."""
        signaller = _Recorder(fail_on={22})
        reloader = NvimReloader(pid_source=lambda: [11, 22, 33], signaller=signaller)

        assert reloader.reload() is False
        assert [pid for pid, _ in signaller.calls] == [11, 22, 33]


class TestNvimReloaderPort:
    def test_implements_the_reloader_port(self) -> None:
        assert isinstance(NvimReloader(pid_source=lambda: []), IDesktopReloader)


class TestNvimReloaderInjection:
    def test_injected_pid_source_and_signaller_are_used_verbatim(self) -> None:
        calls: list[str] = []
        signaller = _Recorder()

        def pid_source() -> list[int]:
            calls.append("pid_source")
            return [7]

        reloader = NvimReloader(pid_source=pid_source, signaller=signaller)

        assert reloader.reload() is True
        assert calls == ["pid_source"]
        assert signaller.calls == [(7, signal.SIGUSR1)]

    def test_defaults_are_the_pgrep_source_and_os_kill(self) -> None:
        reloader = NvimReloader()

        assert reloader._pid_source is _default_pid_source  # type: ignore[attr-defined]
        assert reloader._signaller is os.kill  # type: ignore[attr-defined]


class TestDefaultPidSource:
    def test_parses_pgrep_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[list[str]] = []
        captured_timeout: list[object] = []

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ARG001
            captured.append(list(cmd))
            captured_timeout.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0, "101\n202\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert _default_pid_source() == [101, 202]
        assert captured == [["pgrep", "-x", "nvim"]]
        # Bounded enumeration: the pgrep call must never hang a converge.
        assert captured_timeout == [5]

    def test_no_matches_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ARG001
            return subprocess.CompletedProcess(cmd, 1, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert _default_pid_source() == []

    def test_skips_junk_and_nonpositive_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ARG001
            return subprocess.CompletedProcess(cmd, 0, "abc\n0\n-5\n77\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert _default_pid_source() == [77]

    def test_pgrep_missing_degrades_to_empty_not_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enumeration is best-effort: a missing pgrep must not surface."""

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ARG001
            raise FileNotFoundError("pgrep")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert _default_pid_source() == []

    def test_pgrep_timeout_degrades_to_empty_not_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hung pgrep is bounded and treated as the vacuous case."""

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ARG001
            raise subprocess.TimeoutExpired(cmd, 5)

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert _default_pid_source() == []


class TestNvimReloaderWiring:
    def test_build_reloaders_appends_nvim_after_btop(self) -> None:
        from runtime.adapters.btop_reloader import BtopReloader
        from runtime.adapters.kitty_reloader import KittyReloader
        from runtime.adapters.nvim_reloader import NvimReloader
        from runtime.cli.main import _build_reloaders

        reloaders = _build_reloaders(Path("/tmp/state"), include_terminal=True)

        assert [type(r) for r in reloaders][-3:] == [
            KittyReloader,
            BtopReloader,
            NvimReloader,
        ]
        assert type(reloaders[-1]) is NvimReloader

    def test_build_reloaders_keeps_nvim_when_terminal_is_disabled(self) -> None:
        from runtime.adapters.btop_reloader import BtopReloader
        from runtime.adapters.nvim_reloader import NvimReloader
        from runtime.adapters.terminal_color_applier import TerminalColorApplier
        from runtime.cli.main import _build_reloaders

        reloaders = _build_reloaders(Path("/tmp/state"), include_terminal=False)

        assert not any(type(r) is TerminalColorApplier for r in reloaders)
        assert [type(r) for r in reloaders][-2:] == [BtopReloader, NvimReloader]
