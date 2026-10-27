"""Applied-appearance ledger + live/restart lanes with fake AGS apps (AD-27…30).

No GUI, no real ``ags``, no host process is touched: discovery reads fake
symlinked app dirs, the live lane uses an injected runner, and the restart lane
is an injected recorder.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from runtime.adapters import appearance_ledger as ledger_module
from runtime.adapters.ags_app_refresh import AgsAppRefreshCoordinator
from runtime.adapters.ags_reloader import AgsAppProcess
from runtime.adapters.appearance_ledger import (
    AppearanceLedger,
    appearance_hash_for_state,
    hold_operation_lock,
)
from runtime.domain.ags_apps import ReloadFailure
from runtime.domain.models import (
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
    PaletteArtifacts,
    PaletteEntry,
    WallpaperEntry,
)

WH = "f" * 64
PEH = "a" * 64


def _now_z() -> str:
    return "2026-10-26T19:00:00Z"


def _state() -> DesktopState:
    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=WH,
            source_path="/img/wall.png",
            imported_at=_now_z(),
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=WH,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=PEH,
            source_wallpaper_hash=WH,
            input_template_hash="b" * 64,
            artifact_hashes=PaletteArtifacts(
                colors_yaml="a" * 64,
                colors_conf="b" * 64,
                colors_gtk_css="c" * 64,
                colors_adw_css="d" * 64,
                colors_sequences="e" * 64,
                colors_rasi="f" * 64,
                colors_kitty="1" * 64,
                colors_btop="2" * 64,
            ),
            generated_at=_now_z(),
        ),
        effects=None,
        icons=None,
        applied_at=_now_z(),
    )


class _StateRepo:
    def __init__(self, state: DesktopState) -> None:
        self.state = state

    def load_current(self) -> DesktopState | None:
        return self.state

    def save(self, state: DesktopState) -> None:
        self.state = state


def _manifest(
    *,
    app: str,
    instance: str,
    config_dir: str,
    mode: str = "live",
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "app": app,
        "instance": instance,
        "configDir": config_dir,
        "mode": mode,
        "holdsUnsavedState": False,
    }


def _deploy(spine: Path, link_name: str, manifest: dict[str, object]) -> Path:
    app_dir = spine / "apps" / link_name / "v0.1.0"
    app_dir.mkdir(parents=True)
    (app_dir / "app.manifest.json").write_text(json.dumps(manifest))
    (app_dir / "app.tsx").write_text(
        f'app.start({{ instanceName: "{manifest["instance"]}" }})\n'
    )
    link = spine / "config" / link_name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(app_dir, target_is_directory=True)
    return link


def _process(pid: int, link: Path, instance: str) -> AgsAppProcess:
    return AgsAppProcess(
        pid=pid,
        argv=("ags", "run", "-d", str(link)),
        config_dir=link,
        instance=instance,
    )


class _LiveRunner:
    """Scripted live-lane runner keyed by the ``-i`` instance argument."""

    def __init__(self, script: dict[str, list[object]] | None = None, delay: float = 0.0) -> None:
        self._script = {key: list(value) for key, value in (script or {}).items()}
        self._delay = delay
        self.calls: list[tuple[str, ...]] = []
        self._active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        instance = argv[argv.index("-i") + 1]
        with self._lock:
            self.calls.append(tuple(argv))
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if self._delay:
                time.sleep(self._delay)
            scripted = self._script.get(instance, ['{"ok":true}\n'])
            action = scripted.pop(0) if len(scripted) > 1 else scripted[0]
            if isinstance(action, Exception):
                raise action
            if isinstance(action, tuple):
                if len(action) == 1:
                    returncode, stdout, stderr = 0, str(action[0]), ""
                else:
                    returncode, stdout, stderr = action
            else:
                returncode, stdout, stderr = 0, str(action), ""
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        finally:
            with self._lock:
                self._active -= 1


class _RestartFake:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def restart_deployed_app(self, **kwargs: object) -> bool:
        self.calls.append(kwargs)
        return self.result


def _coordinator(
    tmp_path: Path,
    *,
    live_runner: _LiveRunner,
    live_names: tuple[str, ...] = (),
    processes: tuple[AgsAppProcess, ...] = (),
    restart: _RestartFake | None = None,
    state: DesktopState | None = None,
    run_id: str = "run-1",
) -> AgsAppRefreshCoordinator:
    ags = tmp_path / "fake-ags"
    ags.write_text("#!/bin/sh\nexit 0\n")
    ags.chmod(0o755)
    spine = tmp_path / "install"
    return AgsAppRefreshCoordinator(
        state_root=tmp_path / "state",
        install_spine=spine,
        state_repo=_StateRepo(state or _state()),
        ags_path=ags,
        process_lister=lambda: processes,
        instance_lister=lambda: live_names,
        live_runner=live_runner,
        restart_reloader=restart or _RestartFake(),  # type: ignore[arg-type]
        run_id_factory=lambda: run_id,
        clock=time.monotonic,
    )


def _deploy_live(spine: Path, *, name: str = "ags-clipboard", instance: str = "clipboard") -> Path:
    return _deploy(
        spine,
        name,
        _manifest(app=name.removeprefix("ags-"), instance=instance, config_dir=name),
    )


class TestLiveLane:
    def test_applied_ack_writes_result_and_ledger(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy_live(spine)
        runner = _LiveRunner({"clipboard": [('{"ok":true}\n',)]})
        coordinator = _coordinator(
            tmp_path,
            live_runner=runner,
            live_names=("clipboard",),
            processes=(_process(42, link, "clipboard"),),
        )
        outcome = coordinator.refresh(_state())
        assert outcome.failures == ()
        assert [record.status for record in outcome.records] == ["applied"]
        assert runner.calls == [
            (
                str(tmp_path / "fake-ags"),
                "request",
                "palette-refresh",
                appearance_hash_for_state(_state()),
                "-i",
                "clipboard",
            )
        ]
        ledger = AppearanceLedger(tmp_path / "state").load()
        assert ledger is not None
        assert ledger.apps["clipboard"].status == "applied"
        log_dir = tmp_path / "state" / "logs" / "ags-app-refresh" / "run-1"
        [record_file] = list(log_dir.glob("*.jsonl"))
        record = json.loads(record_file.read_text().splitlines()[0])
        assert record["status"] == "applied"
        assert record["app"] == "clipboard"
        assert record["discoveryIndex"] == 0

    def test_live_lane_is_parallel(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        first = _deploy_live(spine, name="ags-clipboard", instance="clipboard")
        second = _deploy_live(spine, name="ags-capture", instance="capture")
        runner = _LiveRunner(delay=0.2)
        coordinator = _coordinator(
            tmp_path,
            live_runner=runner,
            live_names=("clipboard", "capture"),
            processes=(
                _process(1, first, "clipboard"),
                _process(2, second, "capture"),
            ),
        )
        started = time.monotonic()
        outcome = coordinator.refresh(_state())
        elapsed = time.monotonic() - started
        assert runner.max_active == 2
        assert elapsed < 0.35
        assert {record.status for record in outcome.records} == {"applied"}

    def test_deferred_ack_is_retried_on_the_next_trigger(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy_live(spine)
        runner = _LiveRunner(
            {
                "clipboard": [
                    ('{"ok":true,"deferred":"busy"}\n',),
                    ('{"ok":true}\n',),
                ]
            }
        )
        coordinator = _coordinator(
            tmp_path,
            live_runner=runner,
            live_names=("clipboard",),
            processes=(_process(42, link, "clipboard"),),
        )
        first = coordinator.refresh(_state())
        assert first.records[0].status == "deferred"
        assert [failure.status for failure in first.failures] == ["deferred"]
        assert len(runner.calls) == 1
        second = coordinator.refresh(_state())
        assert second.records[0].status == "applied"
        assert len(runner.calls) == 2

    def test_busy_failure_is_retryable_and_no_immediate_retry(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy_live(spine)
        runner = _LiveRunner(
            {
                "clipboard": [
                    ('{"ok":false,"error":"busy","retryable":true}\n',),
                    ('{"ok":true}\n',),
                ]
            }
        )
        coordinator = _coordinator(
            tmp_path,
            live_runner=runner,
            live_names=("clipboard",),
            processes=(_process(42, link, "clipboard"),),
        )
        outcome = coordinator.refresh(_state())
        assert len(runner.calls) == 1
        record = outcome.records[0]
        assert record.status == "failed"
        assert record.retryable is True
        assert record.reason == "busy"
        assert "clipboard" in str(outcome.failures[0])
        ledger = AppearanceLedger(tmp_path / "state").load()
        assert ledger is not None
        assert ledger.apps["clipboard"].status == "failed"
        assert ledger.apps["clipboard"].retryable is True
        coordinator.refresh(_state())
        assert len(runner.calls) == 2

    def test_5s_timeout_is_a_failure(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy_live(spine)
        runner = _LiveRunner(
            {
                "clipboard": [
                    subprocess.TimeoutExpired(cmd="ags", timeout=5.0),
                    ('{"ok":true}\n',),
                ]
            }
        )
        coordinator = _coordinator(
            tmp_path,
            live_runner=runner,
            live_names=("clipboard",),
            processes=(_process(42, link, "clipboard"),),
        )
        outcome = coordinator.refresh(_state())
        record = outcome.records[0]
        assert record.status == "failed"
        assert record.timeout is True
        assert record.retryable is True
        assert "timed out" in record.reason

    @pytest.mark.parametrize(
        "stdout",
        [
            "",
            '{"ok":true}\n{"ok":true}\n',
            "not-json\n",
            '{"ok":"yes"}\n',
            '{"ok":true,"extra":1}\n',
            '{"ok":false,"error":"x"}\n',
        ],
    )
    def test_malformed_ack_is_a_failure(self, tmp_path: Path, stdout: str) -> None:
        spine = tmp_path / "install"
        link = _deploy_live(spine)
        runner = _LiveRunner({"clipboard": [(stdout,)]})
        coordinator = _coordinator(
            tmp_path,
            live_runner=runner,
            live_names=("clipboard",),
            processes=(_process(42, link, "clipboard"),),
        )
        outcome = coordinator.refresh(_state())
        assert outcome.records[0].status == "failed"
        assert outcome.failures

    def test_live_failure_never_restarts(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy_live(spine)
        runner = _LiveRunner(
            {"clipboard": [('{"ok":false,"error":"boom","retryable":true}\n',)]}
        )
        restart = _RestartFake(result=True)
        coordinator = _coordinator(
            tmp_path,
            live_runner=runner,
            live_names=("clipboard",),
            processes=(_process(42, link, "clipboard"),),
            restart=restart,
        )
        outcome = coordinator.refresh(_state())
        assert outcome.records[0].status == "failed"
        assert restart.calls == []

    def test_down_live_app_is_skipped_without_invocation(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy_live(spine)
        runner = _LiveRunner()
        coordinator = _coordinator(tmp_path, live_runner=runner, live_names=())
        outcome = coordinator.refresh(_state())
        assert outcome.records[0].status == "skipped"
        assert "not running" in outcome.records[0].reason
        assert runner.calls == []
        ledger = AppearanceLedger(tmp_path / "state").load()
        assert ledger is not None
        assert ledger.apps["clipboard"].status == "skipped"

    def test_no_op_for_unchanged_appearance(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy_live(spine)
        runner = _LiveRunner()
        restart = _RestartFake()
        coordinator = _coordinator(
            tmp_path,
            live_runner=runner,
            live_names=("clipboard",),
            processes=(_process(42, link, "clipboard"),),
            restart=restart,
        )
        first = coordinator.refresh(_state())
        assert first.no_op is False
        calls = len(runner.calls)
        second = coordinator.refresh(_state())
        assert second.no_op is True
        assert second.failures == ()
        assert len(runner.calls) == calls
        assert restart.calls == []

    def test_changed_icons_hash_refreshes(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy_live(spine)
        runner = _LiveRunner()
        coordinator = _coordinator(
            tmp_path,
            live_runner=runner,
            live_names=("clipboard",),
            processes=(_process(42, link, "clipboard"),),
        )
        coordinator.refresh(_state())
        changed = _state()
        from dataclasses import replace

        from runtime.domain.models import IconsArtifacts, IconsEntry

        changed = replace(
            changed,
            icons=IconsEntry(
                hash_algorithm="sha256",
                kind="icons",
                entry_hash="c" * 64,
                source_palette_hash=PEH,
                input_templates_hash="d" * 64,
                input_mappings_hash="e" * 64,
                artifact_hashes=IconsArtifacts(**{"icon.svg": "f" * 64}),
                generated_at=_now_z(),
            ),
        )
        changed_coordinator = _coordinator(
            tmp_path,
            live_runner=runner,
            live_names=("clipboard",),
            processes=(_process(42, link, "clipboard"),),
            state=changed,
            run_id="run-2",
        )
        second = changed_coordinator.refresh(changed)
        assert second.no_op is False
        assert len(runner.calls) == 2


class TestRestartLane:
    def test_down_restart_app_is_launched(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy(
            spine,
            "ags",
            _manifest(app="ags", instance="ags", config_dir="ags", mode="restart"),
        )
        restart = _RestartFake(result=True)
        coordinator = _coordinator(
            tmp_path,
            live_runner=_LiveRunner(),
            live_names=(),
            restart=restart,
        )
        outcome = coordinator.refresh(_state())
        assert [record.status for record in outcome.records] == ["applied"]
        assert restart.calls == [
            {
                "instance": "ags",
                "config_dir": link,
                "argv": [str(tmp_path / "fake-ags"), "run", "-d", str(link)],
                "old_pids": (),
            }
        ]

    def test_restart_failure_is_retried_next_trigger(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags",
            _manifest(app="ags", instance="ags", config_dir="ags", mode="restart"),
        )
        restart = _RestartFake(result=False)
        coordinator = _coordinator(
            tmp_path,
            live_runner=_LiveRunner(),
            live_names=(),
            restart=restart,
        )
        first = coordinator.refresh(_state())
        assert first.records[0].status == "failed"
        assert first.failures
        second = coordinator.refresh(_state())
        assert second.records[0].status == "failed"
        assert len(restart.calls) == 2

    def test_restart_lane_is_serialized(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags",
            _manifest(app="ags", instance="ags", config_dir="ags", mode="restart"),
        )
        _deploy(
            spine,
            "ags-other",
            _manifest(app="other", instance="other", config_dir="ags-other", mode="restart"),
        )

        class _SerialRestart:
            def __init__(self) -> None:
                self.active = 0
                self.max_active = 0
                self.calls: list[str] = []

            def restart_deployed_app(self, **kwargs: object) -> bool:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.calls.append(str(kwargs["instance"]))
                time.sleep(0.03)
                self.active -= 1
                return True

        restart = _SerialRestart()
        coordinator = _coordinator(
            tmp_path,
            live_runner=_LiveRunner(),
            live_names=(),
            restart=restart,  # type: ignore[arg-type]
        )
        outcome = coordinator.refresh(_state())
        assert restart.max_active == 1
        assert {record.status for record in outcome.records} == {"applied"}
        assert len(outcome.records) == 2

    def test_invalid_manifest_is_a_structured_failure(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy(
            spine,
            "ags-broken",
            _manifest(app="broken", instance="broken", config_dir="ags-broken"),
        )
        (link / "app.manifest.json").write_text("{broken")
        coordinator = _coordinator(tmp_path, live_runner=_LiveRunner(), live_names=())
        outcome = coordinator.refresh(_state())
        assert outcome.records == ()
        assert outcome.failures
        failure = outcome.failures[0]
        assert failure.app == "ags-broken"
        assert failure.status == "failed"
        assert "not valid JSON" in failure.reason


class TestLedgerAndLock:
    def test_ledger_write_is_atomic_on_replace_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ledger = AppearanceLedger(tmp_path)
        from runtime.domain.ags_apps import AppAppearanceState, AppliedAppearance

        first = AppliedAppearance(
            version=1,
            appearance_hash="a" * 64,
            operation_id="run-1",
            completed_at=_now_z(),
            apps={
                "clipboard": AppAppearanceState(
                    app="clipboard",
                    instance="clipboard",
                    mode="live",
                    status="applied",
                    appearance_hash="a" * 64,
                )
            },
        )
        ledger.save(first)
        original = ledger.path.read_bytes()

        def _boom(src: str, dst: str) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr(ledger_module.os, "replace", _boom)
        second = AppliedAppearance(
            version=1,
            appearance_hash="b" * 64,
            operation_id="run-2",
            completed_at=_now_z(),
            apps={},
        )
        with pytest.raises(OSError):
            ledger.save(second)
        assert ledger.path.read_bytes() == original
        assert not list(tmp_path.glob(".appearance-ledger-*.tmp"))

    def test_operation_lock_serializes_two_refreshes(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy_live(spine)
        runner = _LiveRunner(delay=0.1)
        _coordinator(
            tmp_path,
            live_runner=runner,
            live_names=("clipboard",),
            processes=(_process(42, link, "clipboard"),),
        )
        retry = _LiveRunner(delay=0.1)
        wait_coordinator = _coordinator(
            tmp_path,
            live_runner=retry,
            live_names=("clipboard",),
            processes=(_process(42, link, "clipboard"),),
        )
        done = threading.Event()

        def _refresh() -> None:
            wait_coordinator.refresh(_state())
            done.set()

        with hold_operation_lock(tmp_path / "state"):
            thread = threading.Thread(target=_refresh)
            thread.start()
            time.sleep(0.15)
            assert not done.is_set()
            assert retry.calls == []
        thread.join(timeout=5)
        assert done.is_set()
        assert len(retry.calls) == 1

    def test_ledger_retries_skipped_app_on_next_trigger(self, tmp_path: Path) -> None:
        ledger = AppearanceLedger(tmp_path)
        from runtime.domain.ags_apps import AppAppearanceState, AppliedAppearance

        ledger.save(
            AppliedAppearance(
                version=1,
                appearance_hash="a" * 64,
                operation_id="run-1",
                completed_at=_now_z(),
                apps={
                    "clipboard": AppAppearanceState(
                        app="clipboard",
                        instance="clipboard",
                        mode="live",
                        status="skipped",
                        appearance_hash="a" * 64,
                        reason="live app is not running",
                    )
                },
            )
        )
        assert ledger.needs_refresh("clipboard", "a" * 64) is True


def test_reload_failure_is_structured_and_string_compatible() -> None:
    failure = ReloadFailure(
        "clipboard",
        instance="clipboard",
        mode="live",
        status="failed",
        reason="timeout",
        detail="5 s",
    )
    assert failure.app == "clipboard"
    assert failure.instance == "clipboard"
    assert failure.mode == "live"
    assert failure.status == "failed"
    assert failure.reason == "timeout"
    assert failure.detail == "5 s"
    assert "clipboard (clipboard, live)" in str(failure)
    assert "clipboard" in f"{failure}"
    assert failure.to_dict()["app"] == "clipboard"
