"""Integration test: a REAL btop picks up a swapped theme after SIGUSR2.

This is the only test that proves the whole point of the adapter — btop is
running, its theme file is replaced behind its back, and the running process
adopts the new colours without a restart.

The negative control is mandatory: without it, the assertion "the new colour
appeared" could be satisfied by anything (a redraw, a coincidence, a stale
read). With the file swapped and NO signal, the new colour must be ABSENT.
Only the signal can make it appear.

PITFALLS this test exists partly to encode (both were hit for real while
investigating):
  - ``XDG_CONFIG_HOME`` must be the PARENT of the ``btop`` dir. btop appends
    ``btop`` itself, so pointing it at ``<root>/config/btop`` makes it look for
    ``<root>/config/btop/btop/``, never read the config, and silently run
    ``Default_theme`` — a convincing false positive.
  - btop needs a real terminal size. Without a TIOCSWINSZ it emits nothing but
    errors.
  - assert on 48;2 BACKGROUND sequences only. Gradients interpolate 101 values
    per family, so a whole-frame colour-set comparison differs between any two
    captures even with no theme change.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path

import pytest

from runtime.adapters.btop_reloader import BtopReloader

pytestmark = pytest.mark.skipif(
    shutil.which("btop") is None, reason="btop not installed; skipping live-process test"
)

BG_A = "#010203"
BG_B = "#abcdef"


def _full_theme(bg: str) -> str:
    """A complete 48-key theme so nothing falls back to Default_theme."""
    out = f'theme[main_bg]="{bg}"\n'
    for key in (
        "main_fg",
        "title",
        "hi_fg",
        "selected_bg",
        "selected_fg",
        "inactive_fg",
        "graph_text",
        "meter_bg",
        "proc_misc",
        "cpu_box",
        "mem_box",
        "net_box",
        "proc_box",
        "div_line",
    ):
        out += f'theme[{key}]="#c0c0c0"\n'
    for grad in (
        "temp",
        "cpu",
        "free",
        "cached",
        "available",
        "used",
        "download",
        "upload",
        "process",
    ):
        for stop in ("start", "mid", "end"):
            out += f'theme[{grad}_{stop}]="#808080"\n'
    for key in (
        "proc_pause_bg",
        "proc_follow_bg",
        "proc_banner_bg",
        "proc_banner_fg",
        "followed_bg",
        "followed_fg",
    ):
        out += f'theme[{key}]="#606060"\n'
    return out


class _Btop:
    """A live btop on a private pty with an isolated config + state root."""

    def __init__(self, root: Path) -> None:
        cfg_home = root / "config"  # PARENT of btop/ -- see module docstring
        self.themes = cfg_home / "btop" / "themes"
        self.themes.mkdir(parents=True)
        self.state = root / "state"
        self.state.mkdir()
        self.theme = self.themes / "csg.theme"
        self.theme.write_text(_full_theme(BG_A))
        (cfg_home / "btop" / "btop.conf").write_text(
            'color_theme = "csg.theme"\nlog_level = "DEBUG"\nupdate_ms = 2000\n'
        )
        env = dict(
            os.environ,
            XDG_CONFIG_HOME=str(cfg_home),
            XDG_STATE_HOME=str(self.state),
            TERM="xterm-256color",
            COLORTERM="truecolor",
        )
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        self.proc = subprocess.Popen(
            ["btop", "--no-tty"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
        )
        os.close(slave)
        self._chunks: list[tuple[float, bytes]] = []

    def pump(self, seconds: float) -> None:
        """Read available output for `seconds`, timestamping each chunk."""
        os.set_blocking(self.master, False)
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except (BlockingIOError, OSError):
                return
            if not chunk:
                return
            self._chunks.append((time.time(), chunk))

    def backgrounds_since(self, since: float) -> set[str]:
        return {
            f"#{int(r):02x}{int(g):02x}{int(b):02x}"
            for ts, data in self._chunks
            if ts >= since
            for r, g, b in re.findall(rb"48;2;(\d+);(\d+);(\d+)", data)
        }

    def theme_loads(self) -> list[str]:
        log = self.state / "btop.log"
        if not log.exists():
            return []
        return [
            line.split("Loading theme file: ")[-1]
            for line in log.read_text(errors="replace").splitlines()
            if "Loading theme file:" in line
        ]

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        try:
            os.close(self.master)
        except OSError:
            pass


def test_btop_adopts_swapped_theme_only_after_sigusr2() -> None:
    """The whole point: a RUNNING btop re-themes on SIGUSR2, live.

    Also asserts the adapter really signals THAT process: the pid source is
    injected to return exactly this btop's pid, so a vacuous pass is impossible.
    """
    with tempfile.TemporaryDirectory(prefix="btop-reload-") as td:
        btop = _Btop(Path(td))

        try:
            time.sleep(3)
            btop.pump(2)
            first = btop.backgrounds_since(time.time() - 2)
            assert BG_A in first, f"theme A not painted on start: {sorted(first)}"
            assert btop.theme_loads(), "btop did not log a theme load; config was not read"

            # Swap the file behind btop's back, exactly as a wallpaper change
            # repoints the consumer pointer at a new cache entry.
            btop.theme.write_text(_full_theme(BG_B))
            time.sleep(3)
            btop.pump(2)
            without_signal = btop.backgrounds_since(time.time() - 2)

            # NEGATIVE CONTROL: btop has no theme watcher. Without the signal
            # the new colour must NOT appear. If this ever passes, the
            # assertion below proves nothing.
            assert BG_B not in without_signal, (
                "btop picked up the theme without a signal -- the negative "
                "control is broken, so the positive result would be meaningless"
            )

            # Signal THIS process through the adapter under test.
            signalled: list[tuple[int, int]] = []
            reloader = BtopReloader(
                pid_source=lambda: [btop.proc.pid],
                signaller=lambda pid, sig: signalled.append((pid, sig)),
            )
            assert reloader.reload() is True
            assert signalled == [(btop.proc.pid, __import__("signal").SIGUSR2)]

            # Now the real signal, via os.kill, and observe the frame.
            time.sleep(0.5)
            import signal as _signal

            os.kill(btop.proc.pid, _signal.SIGUSR2)
            time.sleep(2)
            btop.pump(3)
            after = btop.backgrounds_since(time.time() - 2)

            assert BG_B in after, (
                f"SIGUSR2 did not make the running btop adopt the new theme: {sorted(after)}"
            )
            assert len(btop.theme_loads()) >= 2, (
                "expected a second 'Loading theme file' line proving "
                "updateThemes()+setTheme() ran, not merely a redraw"
            )
        finally:
            btop.close()


def test_reloader_reports_vacuous_when_no_btop_runs() -> None:
    """No btop running is success, not failure."""
    assert BtopReloader(pid_source=lambda: []).reload() is True


def test_reloader_enumerates_the_real_btop_process() -> None:
    """The default pid source must actually find a live btop we spawned."""
    with tempfile.TemporaryDirectory(prefix="btop-pids-") as td:
        btop = _Btop(Path(td))
        try:
            time.sleep(2)
            from runtime.adapters.btop_reloader import _default_pid_source

            assert btop.proc.pid in _default_pid_source()
        finally:
            btop.close()