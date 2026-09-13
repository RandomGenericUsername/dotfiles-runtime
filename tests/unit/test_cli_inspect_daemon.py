"""P5-1-4 observability & safety surface tests (AD-41).

Covers the read-only daemon status surface (``inspect daemon``) — present,
absent, and bus-unavailable — plus the kill-switch release path and the
trigger-logged automatic actions (reactive converge + prune, including the
daemon-path prune delete-audit line). No live bus/systemd is required: the
D-Bus probe seam is injected/faked.
"""

from __future__ import annotations

import hashlib
import json
import signal
from pathlib import Path

import pytest
from typer.testing import CliRunner

import runtime.adapters.daemon_status as daemon_status
import runtime.cli.main as cli_main
from runtime.adapters.daemon_status import (
    DaemonStatusSnapshot,
    assemble_daemon_report,
)
from runtime.adapters.watch_roots import enumerate_watch_roots
from runtime.cli.main import app
from runtime.domain.models import (
    DesktopState,
    PaletteEntry,
    WallpaperEntry,
)
from runtime.ports.bus_name_owner import IBusNameOwner

runner = CliRunner()
TS = "2026-09-10T00:00:00Z"
WP = "c" * 64
ACTIVE = "a" * 64


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))


def _state_root() -> Path:
    return cli_main._resolve_state_root()


def _h(n: int) -> str:
    return f"{n:064x}"


def _ts(n: int) -> str:
    return f"2026-09-{n:02d}T00:00:00Z"


# ── read-only status assembly ────────────────────────────────────────────


class TestAssembleReport:
    def test_absent_daemon_is_reduced_functionality(self, tmp_path: Path) -> None:
        report = assemble_daemon_report(
            DaemonStatusSnapshot(bus_available=True, name_owned=False, detail="daemon absent"),
            backstop_record=None,
            backstop_path=tmp_path / "last-converged.json",
            watch_roots=(),
        )
        assert report.name_owned is False
        assert report.reduced_functionality is True
        assert report.epoch is None
        assert report.backstop.present is False
        assert report.backstop.input_hash is None

    def test_present_daemon_carries_epoch_and_jobs(self, tmp_path: Path) -> None:
        from runtime.adapters.converge_backstop import BackstopRecord

        report = assemble_daemon_report(
            DaemonStatusSnapshot(
                bus_available=True,
                name_owned=True,
                epoch=7,
                active_jobs={"j1": "capture"},
            ),
            backstop_record=BackstopRecord(input_hash="ab" * 32, converged_at=TS),
            backstop_path=tmp_path / "last-converged.json",
            watch_roots=(),
        )
        assert report.name_owned is True
        assert report.reduced_functionality is False
        assert report.epoch == 7
        assert report.active_jobs == {"j1": "capture"}
        assert report.backstop.present is True
        assert report.backstop.input_hash == "ab" * 32
        assert report.backstop.converged_at == TS

    def test_watch_roots_are_rendered(self) -> None:
        roots = enumerate_watch_roots(
            cli_main._resolve_install_spine(), cli_main._resolve_desired_path()
        )
        report = assemble_daemon_report(
            DaemonStatusSnapshot(bus_available=False, name_owned=False),
            backstop_record=None,
            backstop_path=Path("/state/last-converged.json"),
            watch_roots=roots,
        )
        assert len(report.watch_roots) == 5
        kinds = {root.kind for root in report.watch_roots}
        assert kinds == {"directory", "file"}


class TestProbeDegradation:
    def test_bus_unavailable_is_never_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_bus(*args: object, **kwargs: object) -> object:
            raise OSError("no session bus")

        monkeypatch.setattr(daemon_status, "open_dbus_connection", _no_bus)
        snapshot = daemon_status.probe_session_bus()
        assert snapshot.bus_available is False
        assert snapshot.name_owned is False
        assert snapshot.detail is not None


# ── CLI surface ──────────────────────────────────────────────────────────


class TestInspectDaemonCommand:
    def test_absent_daemon_exits_zero_and_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            daemon_status,
            "probe_session_bus",
            lambda timeout=5.0: DaemonStatusSnapshot(
                bus_available=True, name_owned=False, detail="daemon absent / reduced functionality"
            ),
        )
        result = runner.invoke(app, ["inspect", "daemon"])
        assert result.exit_code == 0
        assert "absent / reduced functionality" in result.output
        assert "watch roots" in result.output

    def test_present_daemon_reports_epoch_jobs_and_backstop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _state_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "last-converged.json").write_text(
            json.dumps(
                {"version": 1, "input_hash": "ab" * 32, "converged_at": TS}
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            daemon_status,
            "probe_session_bus",
            lambda timeout=5.0: DaemonStatusSnapshot(
                bus_available=True,
                name_owned=True,
                epoch=4,
                active_jobs={"job-1": "capture"},
            ),
        )
        result = runner.invoke(app, ["inspect", "daemon", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["daemon_present"] is True
        assert payload["reduced_functionality"] is False
        assert payload["epoch"] == 4
        assert payload["active_jobs"] == {"job-1": "capture"}
        assert payload["last_converged"]["present"] is True
        assert payload["last_converged"]["input_hash"] == "ab" * 32
        assert payload["last_converged"]["converged_at"] == TS
        assert len(payload["watch_roots"]) == 5

    def test_no_daemon_required_for_the_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Full CLI -> real probe -> assembly path with no session bus: the
        # probe must degrade, not raise, and the command exits 0.
        def _no_bus(*args: object, **kwargs: object) -> object:
            raise OSError("no session bus")

        monkeypatch.setattr(daemon_status, "open_dbus_connection", _no_bus)
        result = runner.invoke(app, ["inspect", "daemon"])
        assert result.exit_code == 0
        assert "bus: unavailable" in result.output


# ── kill switch (AD-41) ──────────────────────────────────────────────────


class _FakeOwner(IBusNameOwner):
    def __init__(self) -> None:
        self.releases = 0
        self.acquired = False

    def acquire(self) -> None:
        self.acquired = True

    def release(self) -> None:
        self.releases += 1

    def wait_until_terminated(self) -> None:
        return None


class TestKillSwitch:
    def test_no_self_managed_stop_subcommand(self) -> None:
        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code != 0

    def test_sigterm_handler_releases_the_name(self) -> None:
        owner = _FakeOwner()
        restore = cli_main._install_release_handlers(owner)
        try:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
            assert owner.releases >= 1
        finally:
            restore()
        assert signal.getsignal(signal.SIGTERM) is not handler


# ── trigger-logged automatic actions (AD-41) ─────────────────────────────


def _write_palette(state_root: Path, entry_hash: str, ts: str) -> Path:
    d = state_root / "cache" / "palettes" / entry_hash
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "hash_algorithm": "sha256",
                "kind": "palette",
                "generated_at": ts,
                "artifact_hashes": {"colors.yaml": hashlib.sha256(b"y").hexdigest()},
            }
        ),
        encoding="utf-8",
    )
    (d / "colors.yaml").write_text("y", encoding="utf-8")
    return d


def _seed_prunable() -> Path:
    """Active palette + 7 old palettes so the daemon prune has work to do."""
    from runtime.adapters.json_state_repository import JsonStateRepository

    state_root = _state_root()
    _write_palette(state_root, ACTIVE, _ts(20))
    for i in range(1, 8):
        _write_palette(state_root, _h(i), _ts(i))
    JsonStateRepository(state_root=state_root).save(
        DesktopState(
            schema_version=2,
            wallpaper=WallpaperEntry(
                hash_algorithm="sha256",
                kind="wallpaper",
                content_hash=WP,
                source_path="/img/w.png",
                imported_at=_ts(20),
            ),
            monitors={},
            palette=PaletteEntry(
                hash_algorithm="sha256",
                kind="palette",
                entry_hash=ACTIVE,
                source_wallpaper_hash=WP,
                input_template_hash="t" * 64,
                artifact_hashes={},
                generated_at=_ts(20),
            ),
            effects=None,
            icons=None,
            applied_at=_ts(20),
        )
    )
    return state_root


class TestTriggerLoggedActions:
    def test_reactive_converge_logs_trigger_and_outcome(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _seed_prunable()
        monkeypatch.setattr(cli_main, "_run_check_inputs", lambda: None)
        monkeypatch.setattr(cli_main, "_run_regenerate_stale", lambda **_: None)
        monkeypatch.setattr(cli_main, "_run_reconcile", lambda **_: None)
        monkeypatch.setattr(cli_main, "_run_converge", lambda **_: None)
        monkeypatch.setattr(cli_main, "_run_prune", lambda **_: None)
        with caplog.at_level("INFO", logger="runtime.cli.main"):
            cli_main._run_reactive_converge(observe_only=False, source="change")
        assert any(
            "automatic action: trigger=reactive action=converge outcome=converged" in r.message
            for r in caplog.records
        )
        record = next(
            r
            for r in caplog.records
            if getattr(r, "trigger", None) == "reactive"
        )
        assert record.action == "converge"
        assert record.outcome == "converged"

    def test_daemon_prune_writes_audit_line_and_logs_trigger(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The daemon-initiated prune path writes trigger='prune' with counts."""
        from runtime.application.inspect import InspectHistoryUseCase

        state_root = _seed_prunable()
        monkeypatch.setattr(cli_main, "_run_check_inputs", lambda: None)
        monkeypatch.setattr(cli_main, "_run_regenerate_stale", lambda **_: None)
        monkeypatch.setattr(cli_main, "_run_reconcile", lambda **_: None)
        monkeypatch.setattr(cli_main, "_run_converge", lambda **_: None)
        with caplog.at_level("INFO", logger="runtime.cli.main"):
            result = cli_main._run_reactive_converge(observe_only=False, source="change")

        assert result.ran is True
        records = InspectHistoryUseCase(state_root).run(limit=0)
        prune_records = [r for r in records if r.trigger == "prune"]
        assert len(prune_records) == 1
        assert prune_records[0].details is not None
        assert prune_records[0].details["removed"] >= 1
        assert prune_records[0].details["layers"]["palettes"] >= 1
        assert any(r.trigger == "reactive" for r in records)
        assert any(
            "automatic action: trigger=prune action=prune" in r.message for r in caplog.records
        )

    def test_manual_prune_logs_trigger(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _seed_prunable()
        with caplog.at_level("INFO", logger="runtime.cli.main"):
            cli_main._run_prune(dry_run=False, keep=5, prune_pinned=False)
        assert any(
            "automatic action: trigger=prune action=prune outcome=removed" in r.message
            for r in caplog.records
        )
