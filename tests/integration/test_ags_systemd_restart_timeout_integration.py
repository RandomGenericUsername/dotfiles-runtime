"""Integration proof: a slow but healthy systemd restart is not a failure.

The fake ``systemctl`` below takes 15 s to finish its restart job (well past
the old 10 s subprocess bound) and then marks the AGS instance ready. The
adapter must wait for the job with the configured 90 s bound and confirm the
new bus owner, instead of reporting code 124.
"""

from __future__ import annotations

import stat
import time
from pathlib import Path

import pytest

from runtime.adapters.ags_reloader import BAR_INSTANCE_NAME, AgsReloader
from runtime.adapters.ags_systemd_units import AgsSystemdUnit

pytestmark = pytest.mark.integration


class _ReadyWatcher:
    """Owner event seam backed by the fake systemctl's ready marker."""

    def __init__(self, marker: Path) -> None:
        self._marker = marker

    def wait_for_owner(self, name: str, *, timeout: float | None = None) -> bool:
        return self._marker.is_file()


def test_slow_15s_restart_succeeds(tmp_path: Path) -> None:
    marker = tmp_path / "ready"
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "sleep 15\n"
        f': > "{marker}"\n'
        "exit 0\n"
    )
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IEXEC)
    config = tmp_path / "install" / "config" / "ags"
    config.mkdir(parents=True)
    unit = AgsSystemdUnit("dotfiles-ags-bar.service", "ags", config)
    reloader = AgsReloader(
        systemd_unit_lister=lambda: (unit,),
        systemctl=str(systemctl),
        name_watcher=_ReadyWatcher(marker),
        launch_lock_path=tmp_path / "ags-launch.lock",
    )

    started = time.monotonic()
    result = reloader.restart_deployed_app(instance=BAR_INSTANCE_NAME, config_dir=config)
    elapsed = time.monotonic() - started

    assert result.ok is True, result.error
    assert result.argv == (str(systemctl), "--user", "restart", unit.unit)
    assert elapsed >= 15.0
