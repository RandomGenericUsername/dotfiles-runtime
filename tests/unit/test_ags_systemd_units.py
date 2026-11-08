"""Systemd-aware AGS restart lane: unit discovery, parsing and activation."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.adapters.ags_reloader import (
    BAR_INSTANCE_NAME,
    AgsReloader,
    AgsRestartResult,
)
from runtime.adapters.ags_systemd_units import (
    DEFAULT_RESTART_TIMEOUT,
    AgsSystemdUnit,
    SystemdCommandResult,
    discover_ags_systemd_units,
    parse_exec_start_config_dir,
    run_systemctl,
    unit_for_config_dir,
)


class _Runner:
    """Scripted systemctl seam keyed by exact argv."""

    def __init__(self, responses: dict[tuple[str, ...], SystemdCommandResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: object) -> SystemdCommandResult:
        key = tuple(argv)  # type: ignore[arg-type]
        self.calls.append(key)
        return self.responses.get(key, SystemdCommandResult(1, "", "unexpected command"))


class _FakeProcess:
    def __init__(self, code: int | None) -> None:
        self.returncode = code
        self._code = code

    def poll(self) -> int | None:
        return self._code


class _FakeWatcher:
    """Scripted D-Bus owner seam; records the timeout each wait receives."""

    def __init__(self, *, owned: bool) -> None:
        self._owned = owned
        self.calls: list[tuple[str, float | None]] = []

    def wait_for_owner(self, name: str, *, timeout: float | None = None) -> bool:
        self.calls.append((name, timeout))
        return self._owned


def _ags_bin(tmp_path: Path) -> Path:
    probe = tmp_path / "ags"
    probe.write_text("#!/bin/sh\nexit 0\n")
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    return probe


def _unit_name(app: str) -> str:
    return f"dotfiles-ags-{app}.service"


def _show_value(config_path: Path) -> str:
    return (
        "{ path=/usr/bin/ags-run-locked ; "
        "argv[]=/usr/bin/ags-run-locked --exec --expect-instance ags "
        "--lock-timeout 10 --register-timeout 10 -- "
        f"/usr/bin/ags run -d {config_path} ; ignore_errors=no ; "
        "start_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


def _discovery_runner(config_path: Path, *, unit: str | None = None) -> _Runner:
    name = unit or _unit_name("bar")
    return _Runner(
        {
            (
                "systemctl",
                "--user",
                "list-unit-files",
                "dotfiles-ags-*.service",
                "--no-legend",
                "--plain",
            ): SystemdCommandResult(0, f"{name} enabled\n"),
            (
                "systemctl",
                "--user",
                "show",
                "-p",
                "ExecStart",
                "--value",
                name,
            ): SystemdCommandResult(0, _show_value(config_path)),
        }
    )


class TestExecStartParsing:
    def test_systemd_struct_and_wrapper_argv(self, tmp_path: Path) -> None:
        config = tmp_path / "install" / "config" / "ags"
        assert parse_exec_start_config_dir(_show_value(config)) == config

    def test_plain_command_and_long_directory(self, tmp_path: Path) -> None:
        config = tmp_path / "spine" / "config" / "ags-notifications"
        assert (
            parse_exec_start_config_dir(
                f"/usr/bin/ags run --directory {config} --some-flag"
            )
            == config
        )
        assert parse_exec_start_config_dir(f"/usr/bin/ags run --directory={config}") == config

    def test_non_run_command_has_no_config_dir(self) -> None:
        assert parse_exec_start_config_dir("/usr/bin/ags list") is None
        assert parse_exec_start_config_dir("") is None


class TestUnitDiscovery:
    def test_discovers_unit_and_config_dir_from_exec_start(self, tmp_path: Path) -> None:
        config = tmp_path / "install" / "config" / "ags"
        runner = _discovery_runner(config)
        units = discover_ags_systemd_units(command_runner=runner)

        assert units == (
            AgsSystemdUnit(
                unit=_unit_name("bar"),
                config_dir="ags",
                config_path=config,
            ),
        )
        assert runner.calls[0][2:4] == ("list-unit-files", "dotfiles-ags-*.service")
        assert runner.calls[1][:4] == ("systemctl", "--user", "show", "-p")

    def test_skips_units_without_an_ags_run_target(self, tmp_path: Path) -> None:
        runner = _Runner(
            {
                (
                    "systemctl",
                    "--user",
                    "list-unit-files",
                    "dotfiles-ags-*.service",
                    "--no-legend",
                    "--plain",
                ): SystemdCommandResult(0, f"{_unit_name('bar')} enabled\n"),
                (
                    "systemctl",
                    "--user",
                    "show",
                    "-p",
                    "ExecStart",
                    "--value",
                    _unit_name("bar"),
                ): SystemdCommandResult(0, "{ path=/usr/bin/true ; argv[]=/usr/bin/true }"),
            }
        )
        assert discover_ags_systemd_units(command_runner=runner) == ()

    def test_no_systemd_session_yields_no_units(self) -> None:
        runner = _Runner({})
        assert discover_ags_systemd_units(command_runner=runner) == ()

    def test_unit_matching_uses_path_then_basename(self, tmp_path: Path) -> None:
        config = tmp_path / "install" / "config" / "ags"
        unit = AgsSystemdUnit(_unit_name("bar"), "ags", config)
        assert unit_for_config_dir((unit,), config) is unit
        assert unit_for_config_dir((unit,), Path("ags")) is unit
        assert unit_for_config_dir((unit,), Path("other")) is None


class TestAgsReloaderUnitRestart:
    def test_unit_managed_restart_uses_only_systemctl(self, tmp_path: Path) -> None:
        ags = _ags_bin(tmp_path)
        config = tmp_path / "install" / "config" / "ags"
        config.mkdir(parents=True)
        unit = AgsSystemdUnit(_unit_name("bar"), "ags", config)
        runner = _Runner(
            {
                ("systemctl", "--user", "restart", unit.unit): SystemdCommandResult(0),
            }
        )
        reloader = AgsReloader(
            ags_path=ags,
            instance_lister=lambda: (BAR_INSTANCE_NAME,),
            systemd_unit_lister=lambda: (unit,),
            systemctl_runner=runner,
            launch_lock_path=tmp_path / "ags-launch.lock",
        )
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run") as mock_run,
            patch("runtime.adapters.ags_reloader.subprocess.Popen") as mock_popen,
        ):
            result = reloader.restart_deployed_app(
                instance=BAR_INSTANCE_NAME,
                config_dir=config,
            )

        assert result == AgsRestartResult(
            ok=True,
            argv=("systemctl", "--user", "restart", unit.unit),
        )
        assert runner.calls == [("systemctl", "--user", "restart", unit.unit)]
        mock_run.assert_not_called()
        mock_popen.assert_not_called()

    def test_unit_restart_failure_never_falls_back_to_raw_launch(
        self, tmp_path: Path
    ) -> None:
        ags = _ags_bin(tmp_path)
        config = tmp_path / "install" / "config" / "ags"
        config.mkdir(parents=True)
        unit = AgsSystemdUnit(_unit_name("bar"), "ags", config)
        runner = _Runner(
            {
                ("systemctl", "--user", "restart", unit.unit): SystemdCommandResult(
                    1, "", "unit failed"
                ),
            }
        )
        reloader = AgsReloader(
            ags_path=ags,
            instance_lister=lambda: (BAR_INSTANCE_NAME,),
            systemd_unit_lister=lambda: (unit,),
            systemctl_runner=runner,
            launch_lock_path=tmp_path / "ags-launch.lock",
        )
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run") as mock_run,
            patch("runtime.adapters.ags_reloader.subprocess.Popen") as mock_popen,
        ):
            result = reloader.restart_deployed_app(
                instance=BAR_INSTANCE_NAME,
                config_dir=config,
            )

        assert result.ok is False
        assert result.argv == ("systemctl", "--user", "restart", unit.unit)
        assert "unit failed" in result.error
        mock_run.assert_not_called()
        mock_popen.assert_not_called()

    def test_unit_restart_must_restore_the_instance(
        self, tmp_path: Path
    ) -> None:
        ags = _ags_bin(tmp_path)
        config = tmp_path / "install" / "config" / "ags"
        config.mkdir(parents=True)
        unit = AgsSystemdUnit(_unit_name("bar"), "ags", config)
        runner = _Runner(
            {
                ("systemctl", "--user", "restart", unit.unit): SystemdCommandResult(0),
            }
        )
        reloader = AgsReloader(
            ags_path=ags,
            instance_lister=lambda: (),
            systemd_unit_lister=lambda: (unit,),
            systemctl_runner=runner,
            launch_lock_path=tmp_path / "ags-launch.lock",
        )
        with patch("runtime.adapters.ags_reloader.time.sleep"):
            result = reloader.restart_deployed_app(
                instance=BAR_INSTANCE_NAME,
                config_dir=config,
            )
        assert result.ok is False
        assert "did not return" in result.error

    def test_reload_does_not_restart_a_unit_managed_bar_twice(self, tmp_path: Path) -> None:
        ags = _ags_bin(tmp_path)
        config = tmp_path / "install" / "config" / "ags"
        config.mkdir(parents=True)
        unit = AgsSystemdUnit(_unit_name("bar"), "ags", config)
        runner = _Runner(
            {
                ("systemctl", "--user", "restart", unit.unit): SystemdCommandResult(0),
            }
        )
        reloader = AgsReloader(
            ags_path=ags,
            app_lister=lambda: (),
            instance_lister=lambda: (BAR_INSTANCE_NAME,),
            bar_pid_lister=lambda: (),
            systemd_unit_lister=lambda: (unit,),
            systemctl_runner=runner,
            launch_lock_path=tmp_path / "ags-launch.lock",
        )
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run") as mock_run,
            patch("runtime.adapters.ags_reloader.subprocess.Popen") as mock_popen,
        ):
            assert reloader.reload() is True
        assert runner.calls == [("systemctl", "--user", "restart", unit.unit)]
        mock_run.assert_not_called()
        mock_popen.assert_not_called()

    def test_non_unit_restart_keeps_the_raw_fallback(self, tmp_path: Path) -> None:
        ags = _ags_bin(tmp_path)
        config = tmp_path / "install" / "config" / "ags-capture"
        config.mkdir(parents=True)
        owned: set[str] = set()

        def _spawn(*args: object, **kwargs: object) -> object:
            owned.add("capture")
            return _FakeProcess(code=None)

        reloader = AgsReloader(
            ags_path=ags,
            instance_lister=lambda: tuple(sorted(owned)),
            systemd_unit_lister=lambda: (),
            launch_lock_path=tmp_path / "ags-launch.lock",
        )
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run"),
            patch(
                "runtime.adapters.ags_reloader.subprocess.Popen",
                side_effect=_spawn,
            ) as mock_popen,
            patch("runtime.adapters.ags_reloader.time.sleep"),
        ):
            result = reloader.restart_deployed_app(instance="capture", config_dir=config)
        assert result.ok is True
        assert result.argv == (str(ags), "run", "-d", str(config))
        mock_popen.assert_called_once_with(
            [str(ags), "run", "-d", str(config)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


class TestSystemctlRestartBound:
    def test_restart_bound_is_at_least_60s(self) -> None:
        assert DEFAULT_RESTART_TIMEOUT >= 60.0

    def test_restart_argv_has_no_wait_operand(self, tmp_path: Path) -> None:
        ags = _ags_bin(tmp_path)
        config = tmp_path / "install" / "config" / "ags"
        config.mkdir(parents=True)
        unit = AgsSystemdUnit(_unit_name("bar"), "ags", config)
        watcher = _FakeWatcher(owned=True)
        runner = _Runner(
            {
                ("systemctl", "--user", "restart", unit.unit): SystemdCommandResult(0),
            }
        )
        reloader = AgsReloader(
            ags_path=ags,
            systemd_unit_lister=lambda: (unit,),
            systemctl_runner=runner,
            name_watcher=watcher,
            launch_lock_path=tmp_path / "ags-launch.lock",
        )
        result = reloader.restart_deployed_app(instance=BAR_INSTANCE_NAME, config_dir=config)

        assert result.ok is True
        assert "--wait" not in result.argv
        assert result.argv == ("systemctl", "--user", "restart", unit.unit)

    @pytest.mark.parametrize("elapsed", [15.0, 25.0])
    def test_slow_healthy_restart_within_bound_succeeds(
        self, tmp_path: Path, elapsed: float
    ) -> None:
        ags = _ags_bin(tmp_path)
        config = tmp_path / "install" / "config" / "ags"
        config.mkdir(parents=True)
        unit = AgsSystemdUnit(_unit_name("bar"), "ags", config)
        watcher = _FakeWatcher(owned=True)
        runner = _Runner(
            {
                ("systemctl", "--user", "restart", unit.unit): SystemdCommandResult(0),
            }
        )
        ticks = iter([100.0, 100.0 + elapsed])
        reloader = AgsReloader(
            ags_path=ags,
            systemd_unit_lister=lambda: (unit,),
            systemctl_runner=runner,
            name_watcher=watcher,
            clock=lambda: next(ticks),
            launch_lock_path=tmp_path / "ags-launch.lock",
        )

        result = reloader.restart_deployed_app(instance=BAR_INSTANCE_NAME, config_dir=config)

        assert result.ok is True
        [call] = watcher.calls
        assert call[0] == BAR_INSTANCE_NAME
        assert call[1] == pytest.approx(DEFAULT_RESTART_TIMEOUT - elapsed)

    def test_never_ready_start_fails_within_configured_bound(self, tmp_path: Path) -> None:
        ags = _ags_bin(tmp_path)
        config = tmp_path / "install" / "config" / "ags"
        config.mkdir(parents=True)
        unit = AgsSystemdUnit(_unit_name("bar"), "ags", config)
        watcher = _FakeWatcher(owned=False)
        runner = _Runner(
            {
                ("systemctl", "--user", "restart", unit.unit): SystemdCommandResult(0),
            }
        )
        ticks = iter([0.0, 0.0])
        reloader = AgsReloader(
            ags_path=ags,
            systemd_unit_lister=lambda: (unit,),
            systemctl_runner=runner,
            name_watcher=watcher,
            systemd_restart_timeout=5.0,
            clock=lambda: next(ticks),
            launch_lock_path=tmp_path / "ags-launch.lock",
        )

        result = reloader.restart_deployed_app(instance=BAR_INSTANCE_NAME, config_dir=config)

        assert result.ok is False
        assert "did not return within 5 s" in result.error
        [call] = watcher.calls
        assert call[1] == pytest.approx(5.0)

    def test_old_owner_is_gone_before_new_owner_is_confirmed(self, tmp_path: Path) -> None:
        ags = _ags_bin(tmp_path)
        config = tmp_path / "install" / "config" / "ags"
        config.mkdir(parents=True)
        unit = AgsSystemdUnit(_unit_name("bar"), "ags", config)
        state = {"old_alive": True, "owner": "old"}

        def _runner(argv: object) -> SystemdCommandResult:
            state["old_alive"] = False
            state["owner"] = "new"
            return SystemdCommandResult(0)

        class _OwnerWatcher:
            def wait_for_owner(
                self, name: str, *, timeout: float | None = None
            ) -> bool:
                assert state["old_alive"] is False
                return state["owner"] == "new"

        reloader = AgsReloader(
            ags_path=ags,
            systemd_unit_lister=lambda: (unit,),
            systemctl_runner=_runner,
            name_watcher=_OwnerWatcher(),
            launch_lock_path=tmp_path / "ags-launch.lock",
        )
        result = reloader.restart_deployed_app(instance=BAR_INSTANCE_NAME, config_dir=config)

        assert result.ok is True
        assert state == {"old_alive": False, "owner": "new"}

    def test_adapter_restart_uses_the_long_bound_not_discovery_10s(
        self, tmp_path: Path
    ) -> None:
        ags = _ags_bin(tmp_path)
        config = tmp_path / "install" / "config" / "ags"
        config.mkdir(parents=True)
        unit = AgsSystemdUnit(_unit_name("bar"), "ags", config)
        watcher = _FakeWatcher(owned=True)
        seen: list[float] = []

        def _run(argv: object, *, timeout: float) -> SystemdCommandResult:
            seen.append(timeout)
            return SystemdCommandResult(0)

        with patch("runtime.adapters.ags_systemd_units.run_systemctl", side_effect=_run):
            reloader = AgsReloader(
                ags_path=ags,
                systemd_unit_lister=lambda: (unit,),
                name_watcher=watcher,
                launch_lock_path=tmp_path / "ags-launch.lock",
            )
            result = reloader.restart_deployed_app(
                instance=BAR_INSTANCE_NAME, config_dir=config
            )

        assert result.ok is True
        assert seen == [DEFAULT_RESTART_TIMEOUT]


class TestRunSystemctlTimeout:
    def test_explicit_timeout_is_passed_to_subprocess(self) -> None:
        seen: list[float] = []

        def _run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.append(kwargs["timeout"])  # type: ignore[arg-type]
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch("runtime.adapters.ags_systemd_units.subprocess.run", side_effect=_run):
            result = run_systemctl(
                ["systemctl", "--user", "restart", "dotfiles-ags-bar.service"],
                timeout=DEFAULT_RESTART_TIMEOUT,
            )

        assert result.returncode == 0
        assert seen == [DEFAULT_RESTART_TIMEOUT]

    def test_discovery_timeout_default_stays_10s(self) -> None:
        seen: list[float] = []

        def _run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.append(kwargs["timeout"])  # type: ignore[arg-type]
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch("runtime.adapters.ags_systemd_units.subprocess.run", side_effect=_run):
            result = run_systemctl(
                ["systemctl", "--user", "list-unit-files", "dotfiles-ags-*.service"]
            )

        assert result.returncode == 0
        assert seen == [10.0]

    def test_a_command_exceeding_its_bound_returns_124(self) -> None:
        def _run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])  # type: ignore[arg-type]

        with patch("runtime.adapters.ags_systemd_units.subprocess.run", side_effect=_run):
            result = run_systemctl(
                ["systemctl", "--user", "restart", "dotfiles-ags-bar.service"],
                timeout=DEFAULT_RESTART_TIMEOUT,
            )

        assert result.returncode == 124
        assert "timed out" in result.stderr
        assert f"{DEFAULT_RESTART_TIMEOUT:g}" in result.stderr
