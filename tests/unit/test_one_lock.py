"""The v3 one-lock contract: only ``converge.lock`` mutates state.

Pins the BUG-015 consolidation: first-run seeding and ``icons regenerate``
share the same kernel flock as wallpaper/skin/mode convergence, no runtime
command creates ``.seed.lock``, and a killed holder releases the lock.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from runtime.adapters.converge_lock import ConvergeLock, ConvergeLockBusyError


def test_no_runtime_command_creates_the_legacy_seed_lock() -> None:
    """Structural pin: production has no seed-mutex class or lock path."""
    root = Path(__file__).resolve().parents[2] / "src" / "runtime"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "FlockSeedMutex" in text:
            offenders.append(f"{path}: FlockSeedMutex")
        # Path construction, not prose: the literal file name paired with a
        # state-root join would create a second lock.
        if 'state_root / ".seed.lock"' in text or 'state_root / \'.seed.lock\'' in text:
            offenders.append(f"{path}: state_root/.seed.lock")
    assert offenders == []


def test_converge_lock_is_busy_without_blocking(tmp_path: Path) -> None:
    with ConvergeLock(tmp_path).hold():
        with pytest.raises(ConvergeLockBusyError, match="convergence already in progress"):
            with ConvergeLock(tmp_path).hold(blocking=False):
                pass  # pragma: no cover - busy path must not enter

    # The same lock is immediately reusable once released.
    with ConvergeLock(tmp_path).hold(blocking=False):
        pass
    assert (tmp_path / "converge.lock").exists()
    assert not (tmp_path / ".seed.lock").exists()


def test_killed_holder_releases_the_kernel_lock(tmp_path: Path) -> None:
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from runtime.adapters.converge_lock import ConvergeLock\n"
        "lock = ConvergeLock(Path(sys.argv[1]))\n"
        "with lock.hold():\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "locked"
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    acquired = threading.Event()

    def _acquire() -> None:
        with ConvergeLock(tmp_path).hold():
            acquired.set()

    thread = threading.Thread(target=_acquire, daemon=True)
    thread.start()
    assert acquired.wait(timeout=5), "a killed holder must not wedge converge.lock"
    thread.join(timeout=5)

    assert not (tmp_path / ".seed.lock").exists()
