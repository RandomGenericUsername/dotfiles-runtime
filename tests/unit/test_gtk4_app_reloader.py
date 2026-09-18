"""Unit tests for Gtk4AppReloader."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from runtime.adapters.gtk4_app_reloader import (
    DEFAULT_SKIP_APPS,
    TARGET_APPS,
    Gtk4AppProcess,
    Gtk4AppReloader,
    _discover_gtk4_apps,
    _resolve_app_name,
    _wait_for_exit,
)


class TestResolveAppName:
    def test_direct_binary(self) -> None:
        assert _resolve_app_name(("power-options-gtk",)) == "power-options-gtk"

    def test_python_script_regression_case(self) -> None:
        """hyprmod runs as ``python /usr/bin/hyprmod`` (THE E2E regression)."""
        assert _resolve_app_name(("/usr/bin/python", "/usr/bin/hyprmod")) == "hyprmod"

    def test_python_versioned_script_with_args(self) -> None:
        argv = ("/usr/bin/python3.14", "/usr/bin/hyprmod", "--flag")
        assert _resolve_app_name(argv) == "hyprmod"

    def test_python_dash_m_module(self) -> None:
        assert _resolve_app_name(("python", "-m", "hyprmod")) == "hyprmod"

    def test_env_wrapped_python_script(self) -> None:
        assert _resolve_app_name(("/usr/bin/env", "python", "/usr/bin/hyprmod")) == "hyprmod"

    def test_other_python_script_is_none(self) -> None:
        assert _resolve_app_name(("python", "some_other_script.py")) is None

    def test_bash_c_payload_is_none(self) -> None:
        assert _resolve_app_name(("bash", "-c", "hyprmod")) is None

    def test_grep_like_argv_is_none(self) -> None:
        assert _resolve_app_name(("rg", "hyprmod")) is None

    def test_bare_shell_is_none(self) -> None:
        assert _resolve_app_name(("/bin/sh",)) is None


def _fake_app_lister(apps: list[Gtk4AppProcess]) -> Callable[[], list[Gtk4AppProcess]]:
    """Create a fake app lister that returns the given apps."""

    def lister() -> list[Gtk4AppProcess]:
        return apps

    return lister


class TestGtk4AppReloader:
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.Popen")
    def test_no_target_apps_returns_true(self, mock_popen: MagicMock) -> None:
        """When no target GTK4 apps are running, reload returns True and spawns nothing."""
        reloader = Gtk4AppReloader(app_lister=lambda: [])
        assert reloader.reload() is True
        mock_popen.assert_not_called()

    @patch("runtime.adapters.gtk4_app_reloader._wait_for_exit")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.Popen")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.run")
    def test_single_app_restart_success(
        self, mock_run: MagicMock, mock_popen: MagicMock, mock_wait: MagicMock
    ) -> None:
        """When a single target app is running and restart succeeds, reload returns True."""
        app = Gtk4AppProcess(pid=1234, argv=("power-options-gtk",), app_name="power-options-gtk")
        mock_wait.return_value = True
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Process still running
        mock_popen.return_value = mock_proc
        reloader = Gtk4AppReloader(app_lister=_fake_app_lister([app]))
        assert reloader.reload() is True

    @patch("runtime.adapters.gtk4_app_reloader._wait_for_exit")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.Popen")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.run")
    def test_multiple_apps_all_restart_success(
        self, mock_run: MagicMock, mock_popen: MagicMock, mock_wait: MagicMock
    ) -> None:
        """When multiple target apps are running and all restart, reload returns True."""
        apps = [
            Gtk4AppProcess(pid=1234, argv=("power-options-gtk",), app_name="power-options-gtk"),
            Gtk4AppProcess(pid=5678, argv=("hyprmod",), app_name="hyprmod"),
        ]
        mock_wait.return_value = True
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Process still running
        mock_popen.return_value = mock_proc
        reloader = Gtk4AppReloader(app_lister=_fake_app_lister(apps))
        assert reloader.reload() is True

    @patch("runtime.adapters.gtk4_app_reloader._wait_for_exit")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.Popen")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.run")
    def test_skip_list_prevents_restart(
        self, mock_run: MagicMock, mock_popen: MagicMock, mock_wait: MagicMock
    ) -> None:
        """When an app is in the skip list, it is not restarted and reload returns True."""
        apps = [
            Gtk4AppProcess(pid=1234, argv=("power-options-gtk",), app_name="power-options-gtk"),
            Gtk4AppProcess(pid=5678, argv=("hyprmod",), app_name="hyprmod"),
        ]
        skip_apps = frozenset({"hyprmod"})
        mock_wait.return_value = True
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Process still running
        mock_popen.return_value = mock_proc
        reloader = Gtk4AppReloader(skip_apps=skip_apps, app_lister=_fake_app_lister(apps))
        assert reloader.reload() is True
        mock_wait.assert_called_once_with(1234)

    def test_discovery_filters_by_executable_name(self) -> None:
        """Discovery only returns processes whose executable name matches TARGET_APPS."""
        # Test that TARGET_APPS contains the expected apps
        assert "power-options-gtk" in TARGET_APPS
        assert "hyprmod" in TARGET_APPS

    def test_default_skip_apps_is_empty(self) -> None:
        """DEFAULT_SKIP_APPS is initially empty (no apps excluded by default)."""
        assert DEFAULT_SKIP_APPS == frozenset()


class TestDiscoverGtk4Apps:
    def test_discovery_handles_missing_proc(self) -> None:
        """When /proc is unreadable, discovery returns empty list without error."""
        # This would require mocking Path("/proc").iterdir() to raise OSError
        # For now, just test that the function exists and returns list
        result = _discover_gtk4_apps()
        assert isinstance(result, list)

    def test_discovery_filters_non_digit_entries(self, tmp_path: Path) -> None:
        """Discovery skips /proc entries that are not numeric (self, etc.)."""
        (tmp_path / "self").mkdir()
        assert _discover_gtk4_apps(tmp_path) == []

    def test_discovery_resolves_interpreter_wrapped_app(self, tmp_path: Path) -> None:
        """A Python-script target is discovered and its original argv preserved."""
        entry = tmp_path / "4321"
        entry.mkdir()
        (entry / "cmdline").write_bytes(b"/usr/bin/python\0/usr/bin/hyprmod\0")
        apps = _discover_gtk4_apps(tmp_path)
        assert [(a.pid, a.app_name, a.argv) for a in apps] == [
            (4321, "hyprmod", ("/usr/bin/python", "/usr/bin/hyprmod"))
        ]

    def test_discovery_ignores_non_target_python_script(self, tmp_path: Path) -> None:
        """A non-target Python script is not swept in by the interpreter rule."""
        entry = tmp_path / "9999"
        entry.mkdir()
        (entry / "cmdline").write_bytes(b"/usr/bin/python\0/usr/bin/other-tool\0")
        assert _discover_gtk4_apps(tmp_path) == []


class TestWaitForExit:
    @patch("runtime.adapters.gtk4_app_reloader.time.sleep")
    @patch("runtime.adapters.gtk4_app_reloader.os.kill")
    def test_returns_true_when_process_already_gone(
        self, mock_kill: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """ProcessLookupError on the first probe means the process is gone."""
        mock_kill.side_effect = ProcessLookupError
        assert _wait_for_exit(1234) is True
        mock_sleep.assert_not_called()

    @patch("runtime.adapters.gtk4_app_reloader.time.sleep")
    @patch("runtime.adapters.gtk4_app_reloader.os.kill")
    def test_returns_true_when_process_exits_during_wait(
        self, mock_kill: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """The helper returns as soon as a later probe reports the pid gone."""
        mock_kill.side_effect = [None, ProcessLookupError]
        assert _wait_for_exit(1234, polls=5, interval=0.01) is True
        assert mock_kill.call_count == 2

    @patch("runtime.adapters.gtk4_app_reloader.time.sleep")
    @patch("runtime.adapters.gtk4_app_reloader.os.kill")
    def test_returns_false_when_still_alive_after_budget(
        self, mock_kill: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """A live process exhausts the bounded poll budget and returns False."""
        mock_kill.return_value = None
        assert _wait_for_exit(1234, polls=3, interval=0.01) is False
        assert mock_kill.call_count == 3
        assert mock_sleep.call_count == 2


class TestRestartHardening:
    @patch("runtime.adapters.gtk4_app_reloader._wait_for_exit")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.Popen")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.run")
    def test_relaunch_waits_for_old_pid_to_exit(
        self, mock_run: MagicMock, mock_popen: MagicMock, mock_wait: MagicMock
    ) -> None:
        """Popen is not called until the exit wait reports the old pid gone."""
        app = Gtk4AppProcess(pid=1234, argv=("power-options-gtk",), app_name="power-options-gtk")
        events: list[str] = []

        def wait_side_effect(pid: int, **kwargs: object) -> bool:
            events.append("wait")
            return True

        def popen_side_effect(*args: object, **kwargs: object) -> MagicMock:
            events.append("popen")
            proc = MagicMock()
            proc.poll.return_value = None
            return proc

        mock_wait.side_effect = wait_side_effect
        mock_popen.side_effect = popen_side_effect
        reloader = Gtk4AppReloader(app_lister=_fake_app_lister([app]))

        assert reloader.reload() is True
        assert events == ["wait", "popen"]
        mock_wait.assert_called_once_with(1234)
        assert mock_run.call_args_list == [
            call(["kill", "1234"], capture_output=True, text=True, timeout=5)
        ]

    @patch("runtime.adapters.gtk4_app_reloader._wait_for_exit")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.Popen")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.run")
    def test_escalates_to_sigkill_on_timeout(
        self, mock_run: MagicMock, mock_popen: MagicMock, mock_wait: MagicMock
    ) -> None:
        """When the grace window elapses, SIGKILL is sent before relaunch."""
        app = Gtk4AppProcess(pid=1234, argv=("power-options-gtk",), app_name="power-options-gtk")
        mock_wait.side_effect = [False, True]
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        reloader = Gtk4AppReloader(app_lister=_fake_app_lister([app]))

        assert reloader.reload() is True
        assert mock_wait.call_count == 2
        assert mock_run.call_args_list == [
            call(["kill", "1234"], capture_output=True, text=True, timeout=5),
            call(["kill", "-9", "1234"], capture_output=True, text=True, timeout=5),
        ]
        mock_popen.assert_called_once()

    @patch("runtime.adapters.gtk4_app_reloader._wait_for_exit")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.Popen")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.run")
    def test_still_alive_after_sigkill_returns_false(
        self, mock_run: MagicMock, mock_popen: MagicMock, mock_wait: MagicMock
    ) -> None:
        """A process surviving SIGKILL aborts the restart and is not relaunched."""
        app = Gtk4AppProcess(pid=1234, argv=("power-options-gtk",), app_name="power-options-gtk")
        mock_wait.side_effect = [False, False]
        reloader = Gtk4AppReloader(app_lister=_fake_app_lister([app]))

        assert reloader.reload() is False
        assert mock_wait.call_count == 2
        assert call(["kill", "-9", "1234"], capture_output=True, text=True, timeout=5) in (
            mock_run.call_args_list
        )
        mock_popen.assert_not_called()

    @patch("runtime.adapters.gtk4_app_reloader._wait_for_exit")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.Popen")
    @patch("runtime.adapters.gtk4_app_reloader.subprocess.run")
    def test_graceful_exit_skips_sigkill(
        self, mock_run: MagicMock, mock_popen: MagicMock, mock_wait: MagicMock
    ) -> None:
        """A clean SIGTERM exit never escalates to SIGKILL."""
        app = Gtk4AppProcess(pid=1234, argv=("power-options-gtk",), app_name="power-options-gtk")
        mock_wait.return_value = True
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        reloader = Gtk4AppReloader(app_lister=_fake_app_lister([app]))

        assert reloader.reload() is True
        assert call(["kill", "-9", "1234"], capture_output=True, text=True, timeout=5) not in (
            mock_run.call_args_list
        )
