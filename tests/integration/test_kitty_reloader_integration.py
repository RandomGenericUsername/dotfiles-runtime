"""Real-process proof for the kitty reloader (change 2.3 / on-host 4.2).

The unit suite injects a signaller; this test exercises the production path:
enumerate a process named ``kitty`` with ``pgrep -x kitty`` and deliver a real
``SIGUSR1`` with ``os.kill``. The fake is a copy of ``/bin/sleep`` under the
name ``kitty``; SIGUSR1's default action terminates it, so process exit is the
observable proof that the signal arrived. The developer's real kitty windows
(if any) also receive SIGUSR1, which is kitty's harmless config-reload signal.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from runtime.adapters.kitty_reloader import KittyReloader

pytestmark = pytest.mark.integration


def _pgrep_kitty() -> list[int]:
    result = subprocess.run(
        ["pgrep", "-x", "kitty"], capture_output=True, text=True, timeout=5
    )
    return [int(token) for token in result.stdout.split() if token.isdigit()]


def _wait_for_name(pid: int, name: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            comm = ""
        if comm == name:
            return
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} never presented comm={name!r}")


def test_real_sigusr1_reaches_a_process_named_kitty(tmp_path: Path) -> None:
    fake = tmp_path / "kitty"
    shutil.copy2("/bin/sleep", fake)
    fake.chmod(0o755)
    proc = subprocess.Popen([str(fake), "300"])
    try:
        _wait_for_name(proc.pid, "kitty")
        pids = _pgrep_kitty()
        assert proc.pid in pids, (
            "pgrep -x kitty did not enumerate the fake kitty; "
            f"pids={pids}, fake={proc.pid}"
        )

        reloader = KittyReloader(pid_source=lambda: pids, signaller=os.kill)
        assert reloader.reload() is True, "signalling the enumerated kitty pids failed"

        # SIGUSR1 terminates sleep by default; wait proves delivery.
        assert proc.wait(timeout=5) == -signal.SIGUSR1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
