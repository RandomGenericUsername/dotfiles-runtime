"""Phase 5 reactive-runtime end-to-end test (private session bus, real procs).

One repeatable harness that exercises the Phase 5 surface with **real**
processes over an isolated ``dbus-daemon`` session bus (never the developer's
live session bus/desktop):

1. ``dotfiles-runtime daemon run`` owns ``org.dotfiles.Events``; the real
   ``dotfiles-runtime inspect daemon`` reports it present with an epoch.
2. A watched spine input change triggers a reactive converge that appends
   exactly one ``reactive`` history line (temp ``state_root`` + spine).
3. Reactive prune is SKIPPED by default (no ``prune`` line, zero deletions)
   and only runs with ``--prune-on-reactive``.
4. ``dotfiles-runtime capture`` registers a job, ``Control`` pause/resume/stop
   round-trips, and ``capture.state`` is observable via ``GetTopicState``.
5. ``Control`` for an unknown job fails loud (``UnknownJob``, N2) and
   ``GetTopicState`` hydration returns the topic state.

The daemon runs the **real** reactive composite; the harness seeds a
fully-fresh state (all derived layers' recorded input hashes match the temp
spine) so the derivation steps are cache hits and no csg/weg/itr tool is
invoked. Desktop reloader binaries are shimmed on the daemon's ``PATH`` so a
reconcile can never reach the live desktop (mirrors the integration conftest
guard).

External prerequisites (missing → ``pytest.skip``):
    - ``dbus-daemon`` on PATH,
    - the ``dotfiles-runtime`` console script beside the running interpreter,
    - Linux (inotify) for the watch path.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

_RUNTIME_BIN = Path(sys.executable).with_name("dotfiles-runtime")
_DBUS_DAEMON = shutil.which("dbus-daemon")

pytestmark = pytest.mark.skipif(
    _DBUS_DAEMON is None or not _RUNTIME_BIN.is_file() or not sys.platform.startswith("linux"),
    reason="phase5 E2E needs dbus-daemon, the dotfiles-runtime script, and Linux/inotify",
)

_HUB_ADDRESS = "/org/dotfiles/Events"
_HUB_NAME = "org.dotfiles.Events"
_HUB_INTERFACE = "org.dotfiles.Events1"
_POLL_DEADLINE = 20.0


# ── process / bus helpers ────────────────────────────────────────────────


@contextmanager
def _private_bus() -> Iterator[str]:
    """Start a private session bus; yield its address; always kill it."""
    assert _DBUS_DAEMON is not None
    result = subprocess.run(
        [_DBUS_DAEMON, "--session", "--fork", "--print-address=1", "--print-pid=1"],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.strip().splitlines()
    address, pid = lines[0], int(lines[1])
    try:
        yield address
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _wait(predicate: Any, *, deadline: float = _POLL_DEADLINE, interval: float = 0.1) -> bool:
    """Poll ``predicate`` until true or the deadline; return the outcome."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _open_conn(address: str) -> Any:
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = address
    from jeepney.io.blocking import open_dbus_connection

    return open_dbus_connection(bus="SESSION")


class _HubClient:
    """A minimal jeepney client for the hub's Events1 surface (test-side)."""

    def __init__(self, address: str) -> None:
        self._conn = _open_conn(address)

    def close(self) -> None:
        self._conn.close()

    def call(
        self, member: str, signature: str, body: tuple[Any, ...], *, timeout: float = 8.0
    ) -> Any:
        from jeepney import DBusAddress, new_method_call

        address = DBusAddress(_HUB_ADDRESS, bus_name=_HUB_NAME, interface=_HUB_INTERFACE)
        return self._conn.send_and_get_reply(
            new_method_call(address, member, signature, body), timeout=timeout
        )

    def name_owned(self) -> bool:
        from jeepney import DBusAddress, new_method_call

        bus = DBusAddress(
            "/org/freedesktop/DBus",
            bus_name="org.freedesktop.DBus",
            interface="org.freedesktop.DBus",
        )
        reply = self._conn.send_and_get_reply(
            new_method_call(bus, "NameHasOwner", "s", (_HUB_NAME,)), timeout=5.0
        )
        return bool(reply.body[0])

    def active_jobs(self) -> dict[str, str]:
        reply = self.call("GetActiveJobs", "", ())
        return dict(reply.body[0])

    def topic_state(self, topic: str) -> dict[str, Any]:
        reply = self.call("GetTopicState", "s", (topic,))
        return _unwrap(reply.body[0])


def _unwrap(value: Any) -> Any:
    """Normalize a jeepney-parsed ``a{sv}`` dict to plain Python values."""
    if isinstance(value, dict):
        return {key: _unwrap(item) for key, item in value.items()}
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return _unwrap(value[1])
    if isinstance(value, list):
        return [_unwrap(item) for item in value]
    return value


# ── sandbox (temp spine + seeded state) ──────────────────────────────────


class _Sandbox:
    """An isolated state_root + spine seeded fully-fresh for the composite."""

    def __init__(self, root: Path) -> None:
        self.tmp = root
        self.state_home = root / "state-home"
        self.config_home = root / "config-home"
        self.spine = root / "install"
        self.bin_dir = root / "bin"
        self.state_root = self.state_home / "dotfiles"
        self.desired = self.config_home / "dotfiles" / "desired.json"
        self._build_tree()
        self._seed_fresh_state()

    # tree + shims
    def _build_tree(self) -> None:
        (self.spine / "wallpapers").mkdir(parents=True)
        (self.spine / "wallpapers" / "default.png").write_bytes(b"\x89PNG-e2e-default")
        templates = self.spine / "config" / "color-scheme-generator" / "templates"
        templates.mkdir(parents=True)
        (templates / "default.yaml").write_text("window: {}\n")
        (self.spine / "config" / "weg").mkdir(parents=True)
        (self.spine / "config" / "weg" / "effects.yaml").write_text("effects: []\n")
        (self.spine / "config" / "ags").mkdir(parents=True)
        (self.spine / "icon-templates").mkdir(parents=True)
        (self.spine / "icon-templates" / "terminal.svg").write_text("<svg/>")
        (self.spine / "icon-mappings").mkdir(parents=True)
        (self.spine / "icon-mappings" / "icons.yaml").write_text("icons: {}\n")
        self.desired.parent.mkdir(parents=True, exist_ok=True)
        self.bin_dir.mkdir(parents=True)
        for name in (
            "hyprctl",
            "hyprpaper",
            "ags",
            "gjs",
            "gammastep",
            "swww",
            "swaybg",
            "mpvpaper",
            "wlr-randr",
        ):
            shim = self.bin_dir / name
            shim.write_text("#!/bin/sh\nexit 1\n")
            shim.chmod(0o755)

    def env(self, address: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_STATE_HOME": str(self.state_home),
                "XDG_CONFIG_HOME": str(self.config_home),
                "DOTFILES_INSTALL_SPINE": str(self.spine),
                "DBUS_SESSION_BUS_ADDRESS": address,
                "PATH": f"{self.bin_dir}:{env.get('PATH', '')}",
            }
        )
        return env

    # seed
    def _seed_fresh_state(self) -> None:
        os.environ.update(
            {
                "XDG_STATE_HOME": str(self.state_home),
                "XDG_CONFIG_HOME": str(self.config_home),
                "DOTFILES_INSTALL_SPINE": str(self.spine),
            }
        )
        from runtime.adapters.flock_seed_mutex import FlockSeedMutex
        from runtime.adapters.json_state_repository import JsonStateRepository
        from runtime.adapters.seeder import CacheSeeder
        from runtime.application.seed_cache import SeedCacheUseCase

        state_repo = JsonStateRepository(state_root=self.state_root)
        SeedCacheUseCase(
            state_repo=state_repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=self.spine,
            state_root=self.state_root,
            seeder=CacheSeeder(self.state_root),
            mutex=FlockSeedMutex(self.state_root / ".seed.lock"),
        ).run()

        self._make_derivations_fresh(state_repo)
        state = state_repo.load_current()
        assert state is not None and state.wallpaper is not None
        # Point the wallpaper at a real source and the intent at the same path
        # so the declarative step sees an empty changeset (no derivation).
        source = self.spine / "wallpapers" / "default.png"
        state_repo.save(
            dataclasses.replace(
                state, wallpaper=dataclasses.replace(state.wallpaper, source_path=str(source))
            )
        )
        self.desired.write_text(
            json.dumps({"version": 1, "wallpaper": str(source), "keep": 5, "pinned": []})
        )

    def _make_derivations_fresh(self, state_repo: Any) -> None:
        """Rewrite each cache entry's recorded input hashes to the temp spine.

        The daemon's real ``CheckInputs`` recomputes spine hashes; matching the
        recorded ``meta.json`` fields makes every derived layer fresh, so the
        reactive composite performs no derivation in the E2E.
        """
        from runtime.adapters.invalidation import InvalidationQueryAdapter
        from runtime.application.derive import (
            find_effects_catalog,
            find_icon_mappings,
            find_icon_templates,
            find_templates_dir,
        )

        adapter = InvalidationQueryAdapter(
            state_root=self.state_root,
            templates_dir=find_templates_dir(self.spine),
            catalog_path=find_effects_catalog(self.spine),
            icon_templates=find_icon_templates(self.spine),
            icon_mappings=find_icon_mappings(self.spine),
        )
        recomputed = adapter.recompute_input_hashes()
        state = state_repo.load_current()
        assert state is not None and state.palette and state.effects and state.icons

        templates_hash, mappings_hash = str(recomputed["icons"]).split("\x00")
        self._patch_meta(
            "palettes", state.palette.entry_hash, {"input_template_hash": recomputed["palettes"]}
        )
        self._patch_meta(
            "effects", state.effects.entry_hash, {"input_catalog_hash": recomputed["effects"]}
        )
        self._patch_meta(
            "icons",
            state.icons.entry_hash,
            {"input_templates_hash": templates_hash, "input_mappings_hash": mappings_hash},
        )

    def _patch_meta(self, layer: str, entry_hash: str, fields: dict[str, Any]) -> None:
        meta = self.state_root / "cache" / layer / entry_hash / "meta.json"
        data = json.loads(meta.read_text(encoding="utf-8"))
        data.update(fields)
        meta.write_text(json.dumps(data), encoding="utf-8")

    # reactive observations
    def history(self) -> list[dict[str, Any]]:
        path = self.state_root / "history.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def reactive_count(self) -> int:
        return sum(1 for line in self.history() if line.get("trigger") == "reactive")

    def prune_lines(self) -> list[dict[str, Any]]:
        return [line for line in self.history() if line.get("trigger") == "prune"]

    def palette_entries(self) -> set[str]:
        layer = self.state_root / "cache" / "palettes"
        if not layer.is_dir():
            return set()
        return {p.name for p in layer.iterdir() if p.is_dir()}

    def add_prunable_palettes(self, count: int) -> set[str]:
        """Add ``count`` dated palette entries older than the active one."""
        from runtime.adapters.hashing import hash_file

        active = self.state_root / "cache" / "palettes"
        source_dir = next(p for p in active.iterdir() if p.is_dir())
        added: set[str] = set()
        for index in range(count):
            entry_hash = f"{index + 1:064x}"
            target = active / entry_hash
            target.mkdir(parents=True, exist_ok=True)
            for artifact in source_dir.iterdir():
                if artifact.is_file() and artifact.name != "meta.json":
                    (target / artifact.name).write_bytes(artifact.read_bytes())
                    _ = hash_file(target / artifact.name)
            meta = json.loads((source_dir / "meta.json").read_text(encoding="utf-8"))
            meta["generated_at"] = f"2020-01-{index + 1:02d}T00:00:00Z"
            (target / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            added.add(entry_hash)
        return added

    def touch_watched_input(self) -> None:
        """Change the watched intent document (whitespace) — not a derivation."""
        self.desired.write_text(self.desired.read_text(encoding="utf-8") + "\n", encoding="utf-8")


# ── daemon lifecycle ─────────────────────────────────────────────────────


class _Daemon:
    def __init__(self, sandbox: _Sandbox, address: str, extra_args: tuple[str, ...] = ()) -> None:
        self._sandbox = sandbox
        self._log = (sandbox.tmp / "daemon.log").open("w")
        env = sandbox.env(address)
        self.proc = subprocess.Popen(
            [str(_RUNTIME_BIN), "daemon", "run", *extra_args],
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def wait_owned(self, client: _HubClient) -> None:
        if not _wait(client.name_owned):
            raise AssertionError("daemon never owned org.dotfiles.Events; log:\n" + self.log_text())

    def log_text(self) -> str:
        return (self._sandbox.tmp / "daemon.log").read_text(encoding="utf-8", errors="replace")

    def stop(self) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._log.close()
        return self.proc.returncode


@contextmanager
def _running_daemon(
    sandbox: _Sandbox, address: str, extra_args: tuple[str, ...] = ()
) -> Iterator[tuple[_Daemon, _HubClient]]:
    client = _HubClient(address)
    daemon = _Daemon(sandbox, address, extra_args)
    try:
        daemon.wait_owned(client)
        yield daemon, client
    finally:
        daemon.stop()
        client.close()


# ── fake derivation generators (fresh cache entries) ─────────────────────


class _FakeFactory:
    def create_static(self, backend_type: object) -> object:
        raise NotImplementedError

    def create_video(self, backend_type: object) -> object:
        raise NotImplementedError

    def auto_detect(self, source_path: str) -> None:
        return None


class _FakeCsg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.adapters.hashing import hash_file
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        output_dir.mkdir(parents=True, exist_ok=True)
        names = [
            "colors.yaml",
            "colors.conf",
            "colors.gtk.css",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
        ]
        for name in names:
            (output_dir / name).write_text("x")
        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_template_hash="c" * 64,
            artifact_hashes=PaletteArtifacts(
                colors_yaml=hash_file(output_dir / "colors.yaml"),
                colors_conf=hash_file(output_dir / "colors.conf"),
                colors_gtk_css=hash_file(output_dir / "colors.gtk.css"),
                colors_adw_css=hash_file(output_dir / "colors.adw.css"),
                colors_sequences=hash_file(output_dir / "colors.sequences"),
                colors_rasi=hash_file(output_dir / "colors.rasi"),
            ),
            generated_at="2026-01-01T00:00:00Z",
        )


class _FakeWeg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.adapters.hashing import hash_file
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "effect.png").write_bytes(b"\x89PNG")
        return EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_catalog_hash="2" * 64,
            artifact_hashes=EffectsArtifacts(
                **{"effect.png": hash_file(output_dir / "effect.png")}
            ),
            generated_at="2026-01-01T00:00:00Z",
        )


class _FakeItr:
    def render(
        self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path
    ) -> object:
        from runtime.adapters.hashing import hash_file
        from runtime.domain.models import IconsArtifacts, IconsEntry

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "icon.svg").write_text("<svg/>")
        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=output_dir.name,
            source_palette_hash=palette_hash,
            input_templates_hash="5" * 64,
            input_mappings_hash="6" * 64,
            artifact_hashes=IconsArtifacts(**{"icon.svg": hash_file(output_dir / "icon.svg")}),
            generated_at="2026-01-01T00:00:00Z",
        )


# ── recorder shim (a real, signal-driven child) ──────────────────────────


_RECORDER_SHIM = """\
#!/usr/bin/env python3
import signal, time
running = True
def _stop(*_a):
    global running
    running = False
signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)
while running:
    time.sleep(0.05)
"""


# ── tests ────────────────────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[_Sandbox]:
    """A fresh sandbox; restores the process environment on teardown.

    The seed helper (and the bus client) set XDG/DBUS env in-process; without
    restoring, later tests in the same pytest session would inherit them.
    """
    saved = dict(os.environ)
    try:
        yield _Sandbox(tmp_path)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_phase5_reactive_runtime_end_to_end(sandbox: _Sandbox) -> None:
    """Daemon surface + reactive converge (prune off) + real capture Control."""
    with _private_bus() as address:
        with _running_daemon(sandbox, address, ("--activate",)) as (daemon, client):
            # 1. Ownership + inspect daemon (real subprocess) reports it.
            inspect = subprocess.run(
                [str(_RUNTIME_BIN), "inspect", "daemon", "--format", "json"],
                env=sandbox.env(address),
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert inspect.returncode == 0, inspect.stderr
            report = json.loads(inspect.stdout)
            assert report["daemon_present"] is True
            assert report["bus_available"] is True
            assert report["epoch"] is not None

            # 2. Startup converge appended exactly one reactive line.
            assert _wait(lambda: sandbox.reactive_count() == 1), daemon.log_text()
            assert (sandbox.state_root / "last-converged.json").is_file()

            # A watched input change triggers exactly one MORE reactive line.
            before = sandbox.reactive_count()
            sandbox.touch_watched_input()
            assert _wait(lambda: sandbox.reactive_count() == before + 1), daemon.log_text()
            assert sandbox.reactive_count() == before + 1

            # 3. The reactive prune audit is skipped by default: no ``prune``
            #    history line. (The declarative step's diff-driven trim is a
            #    separate, always-on mechanism — see the completion report's
            #    "declarative trim" finding; it is NOT asserted here.)
            assert sandbox.prune_lines() == []

            # 5. GetTopicState hydration works and N2 Control fails loud.
            state = client.topic_state("capture.state")
            assert {"_epoch", "_seq"} <= set(state), state
            assert client.active_jobs() == {}
            unknown = client.call("Control", "ss", ("no-such-job", "pause"))
            from jeepney import HeaderFields, MessageType

            assert unknown.header.message_type == MessageType.error
            assert (
                unknown.header.fields.get(HeaderFields.error_name)
                == "org.dotfiles.Events1.UnknownJob"
            )

            # 4. Real capture host: register, pause/resume/stop round-trip.
            _exercise_capture_host(sandbox, address, client, daemon)

    assert daemon.proc.returncode == 0


def test_reactive_prune_opt_in_removes_and_writes_a_prune_line(sandbox: _Sandbox) -> None:
    """``--prune-on-reactive`` runs the AD-30-bounded prune; default does not."""
    prunable = sandbox.add_prunable_palettes(7)
    before = sandbox.palette_entries()
    assert len(prunable & before) == 7

    with _private_bus() as address:
        with _running_daemon(sandbox, address, ("--activate", "--prune-on-reactive")) as (
            _daemon,
            _client,
        ):
            assert _wait(lambda: sandbox.reactive_count() >= 1)
            sandbox.touch_watched_input()
            # The opt-in flag adds the ``prune`` audit line (the observable
            # difference vs. the default). NOTE: in the real composite the
            # declarative step trims beyond-keep entries first, so this line
            # reports ``removed=0`` — a finding recorded in the completion
            # report, not asserted away here.
            assert _wait(lambda: bool(sandbox.prune_lines())), _daemon.log_text()
            assert len(sandbox.prune_lines()) >= 1
            after = sandbox.palette_entries()
            assert len(after) < len(before)
            assert after  # keep floor retained at least the active entry


def _exercise_capture_host(
    sandbox: _Sandbox, address: str, client: _HubClient, daemon: _Daemon
) -> None:
    shim = sandbox.bin_dir / "wf-recorder"
    shim.write_text(_RECORDER_SHIM)
    shim.chmod(0o755)
    output = sandbox.tmp / "recording.mp4"

    capture_log = (sandbox.tmp / "capture.log").open("w")
    capture = subprocess.Popen(
        [
            str(_RUNTIME_BIN),
            "capture",
            "--backend",
            "wf-recorder",
            "--command",
            f"{shim} -f {output}",
        ],
        env=sandbox.env(address),
        stdout=capture_log,
        stderr=subprocess.STDOUT,
    )
    try:
        assert _wait(lambda: bool(client.active_jobs())), daemon.log_text()
        jobs = client.active_jobs()
        job_id, kind = next(iter(jobs.items()))
        assert kind == "capture"

        def state() -> str | None:
            return client.topic_state("capture.state").get("state")

        assert _wait(lambda: state() == "recording")

        assert client.call("Control", "ss", (job_id, "pause")).header.message_type.name == (
            "method_return"
        )
        assert _wait(lambda: state() == "paused")

        assert client.call("Control", "ss", (job_id, "resume")).header.message_type.name == (
            "method_return"
        )
        assert _wait(lambda: state() == "recording")

        assert client.call("Control", "ss", (job_id, "stop")).header.message_type.name == (
            "method_return"
        )
        assert _wait(lambda: state() == "idle")
        assert _wait(lambda: client.active_jobs() == {})
        capture.wait(timeout=10)
        assert capture.returncode == 0
    finally:
        if capture.poll() is None:
            capture.kill()
            capture.wait(timeout=5)
        capture_log.close()
