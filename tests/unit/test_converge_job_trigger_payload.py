"""Regression tests for BUG-010: converge job triggers and event payloads.

The daemon queue executes a job on behalf of a CLI intent. It must never
invent a history trigger (``daemon`` is a manifest source kind, not a trigger)
and a ``wallpaper.state`` publish must always fit the event contract. These
tests also pin that a queue stress (``suppressHistory``) survives the daemon
boundary, and that a publish failure is visible but non-fatal.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import runtime.cli.main as cli_main
from runtime.adapters.emit_validation import (
    MAX_PAYLOAD_BYTES,
    EmitValidator,
    payload_size_bytes,
)
from runtime.adapters.seeder import CacheSeeder
from runtime.domain.history import HISTORY_TRIGGERS, normalize_history_trigger

INTENT_HASH = "a" * 64
WALLPAPER_HASH = "f" * 64


class _RecordingClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []
        self._fail = fail

    def publish(self, topic: str, payload: dict[str, object]) -> None:
        if self._fail:
            raise RuntimeError("Emit failed: org.dotfiles.Events1.PayloadTooLarge")
        self.published.append((topic, dict(payload)))


def _history(state_root: Path) -> list[dict[str, Any]]:
    path = state_root / "history.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _fake_engine_factory(
    state_root: Path, calls: list[dict[str, object]]
) -> Any:
    """Build a fake engine that records history exactly like the real one."""

    def _factory(*_args: object, **kwargs: object) -> Any:
        calls.append(dict(kwargs))
        suppress = bool(kwargs.get("suppress_history"))

        class _Engine:
            def converge(self, intent: Any) -> Any:
                if not suppress:
                    CacheSeeder(state_root).append_history(
                        trigger=intent.trigger,
                        wallpaper_hash=WALLPAPER_HASH,
                        source_path="/img/wall.png",
                    )
                return SimpleNamespace(
                    generation_hash=INTENT_HASH,
                    degraded=False,
                    reconcile=SimpleNamespace(
                        app_refresh_results=(),
                        generation_hash=INTENT_HASH,
                    ),
                )

        return _Engine()

    return _factory


@pytest.fixture
def state_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli_main, "_resolve_state_root", lambda: root)
    monkeypatch.setattr(cli_main, "_resolve_install_spine", lambda: tmp_path / "install")
    monkeypatch.setattr(cli_main, "_resolve_desired_path", lambda: tmp_path / "desired.json")
    return root


def _write_request(state_root: Path, request: dict[str, object]) -> None:
    directory = state_root / "converge-requests"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{INTENT_HASH}.json").write_text(json.dumps(request), encoding="utf-8")


class TestTriggerNormalization:
    @pytest.mark.parametrize("value", HISTORY_TRIGGERS)
    def test_canonical_values_are_preserved(self, value: str) -> None:
        assert normalize_history_trigger(value) == value

    @pytest.mark.parametrize("value", ["daemon", "apply", "", None, 1, ["set"]])
    def test_unknown_values_fall_back_to_reactive(self, value: object) -> None:
        assert normalize_history_trigger(value) == "reactive"

    def test_fallback_must_itself_be_canonical(self) -> None:
        with pytest.raises(ValueError, match="invalid fallback"):
            normalize_history_trigger("set", fallback="daemon")

    def test_daemon_is_not_a_history_trigger(self) -> None:
        assert "daemon" not in HISTORY_TRIGGERS


class TestDaemonConvergeWorker:
    @pytest.mark.parametrize(
        ("job_request", "expected"),
        [
            ({"trigger": "set"}, "set"),
            ({"trigger": "reconcile"}, "reconcile"),
            ({"trigger": "daemon"}, "reactive"),
            ({}, "reactive"),
        ],
    )
    def test_request_trigger_is_preserved_and_invalid_values_fall_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        state_root: Path,
        job_request: dict[str, object],
        expected: str,
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            cli_main,
            "_build_converge_engine",
            _fake_engine_factory(state_root, calls),
        )
        _write_request(state_root, job_request)

        outcome = cli_main._daemon_converge_worker(INTENT_HASH, "job-1")

        assert outcome.exit_code == 0
        assert [line["trigger"] for line in _history(state_root)] == [expected]
        assert all(line["trigger"] in HISTORY_TRIGGERS for line in _history(state_root))

    def test_missing_request_is_a_reactive_daemon_job(
        self, monkeypatch: pytest.MonkeyPatch, state_root: Path
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            cli_main,
            "_build_converge_engine",
            _fake_engine_factory(state_root, calls),
        )

        cli_main._daemon_converge_worker(INTENT_HASH, "job-1")

        assert [line["trigger"] for line in _history(state_root)] == ["reactive"]

    def test_suppress_history_survives_the_daemon_boundary(
        self, monkeypatch: pytest.MonkeyPatch, state_root: Path
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            cli_main,
            "_build_converge_engine",
            _fake_engine_factory(state_root, calls),
        )
        _write_request(
            state_root,
            {
                "trigger": "reconcile",
                "suppressHistory": True,
                "includeTerminal": False,
            },
        )

        cli_main._daemon_converge_worker(INTENT_HASH, "job-1")

        assert _history(state_root) == []
        assert calls[-1]["suppress_history"] is True
        assert calls[-1]["include_terminal"] is False


class TestCliFallback:
    def test_no_daemon_runs_the_same_convergence_and_records_set(
        self, monkeypatch: pytest.MonkeyPatch, state_root: Path, tmp_path: Path
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            cli_main,
            "_build_converge_engine",
            _fake_engine_factory(state_root, calls),
        )

        class _NoDaemonClient:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def submit(self, _intent_hash: str, _request: object) -> None:
                return None

        monkeypatch.setattr(
            "runtime.adapters.dbus_converge_client.DbusConvergeClient",
            _NoDaemonClient,
        )
        image = tmp_path / "wall.png"
        image.write_bytes(b"fallback bytes")

        result = cli_main._converge_cli(trigger="set", wallpaper_path=image)

        assert result.generation_hash == INTENT_HASH
        assert [line["trigger"] for line in _history(state_root)] == ["set"]
        assert calls[-1]["suppress_history"] is False


class TestWallpaperStatePayload:
    def test_oversized_optional_fields_are_trimmed_below_the_contract_cap(self) -> None:
        client = _RecordingClient()

        cli_main._publish_wallpaper_state(
            client,
            "error",
            "f" * 64,
            "set",
            stage="generating_palette",
            reload_failures=[f"failure-{index}:" + "x" * 5000 for index in range(500)],
            error="E" * 1_000_000 + "\nsecond line",
        )

        assert len(client.published) == 1
        topic, payload = client.published[0]
        assert topic == "wallpaper.state"
        assert payload_size_bytes(payload) <= MAX_PAYLOAD_BYTES
        assert len(payload["error"]) <= 512  # type: ignore[arg-type]
        assert len(payload["reload_failures"]) <= 32  # type: ignore[arg-type]
        assert all(len(item) <= 200 for item in payload["reload_failures"])  # type: ignore[union-attr]
        assert "\n" not in payload["error"]  # one-line summary only
        # The same payload is accepted by the hub's own validator.
        EmitValidator().validate(topic, payload, ":1.1")

    def test_unknown_stage_and_trigger_are_omitted_not_rejected(self) -> None:
        client = _RecordingClient()

        cli_main._publish_wallpaper_state(
            client,
            "applying",
            "f" * 64,
            "daemon",
            stage="converging",
        )

        _topic, payload = client.published[0]
        assert "stage" not in payload
        assert "trigger" not in payload
        EmitValidator().validate("wallpaper.state", payload, ":1.1")

    def test_publish_failure_is_non_fatal_and_warning_visible(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _RecordingClient(fail=True)

        with caplog.at_level("WARNING", logger="runtime.cli.main"):
            cli_main._publish_wallpaper_state(
                client, "done", WALLPAPER_HASH, "set", stage="finished"
            )

        records = [record for record in caplog.records if "publish failed" in record.message]
        assert records, caplog.text
        assert records[0].exc_info is None  # no traceback masking the result
        assert "PayloadTooLarge" in records[0].message
