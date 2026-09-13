"""ICME save-event tests + the C1 "not a daemon trigger" ruling (5-4).

Per the validate-gate ruling C1, ``icme.saved`` is a BAR/UI domain event:
it is emitted through the hub for consumers, but the daemon observes ICME
through the watched icon-mappings file (Epic 5-3). A test pins that the
daemon's trigger surface never references the topic.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping

import pytest

import runtime.cli.main as cli_main
from runtime.adapters import bar_subscriber as bs
from runtime.application.icme import ICME_SAVED_TOPIC, emit_icme_saved
from runtime.domain.hub import KNOWN_TOPICS
from runtime.ports.event_bus import IEventPublisher


class _RecordingPublisher(IEventPublisher):
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        self.published.append((topic, dict(payload)))


class TestEmitIcmeSaved:
    def test_publishes_contract_payload(self) -> None:
        publisher = _RecordingPublisher()
        emit_icme_saved(publisher, "/spine/icon-mappings/icons.yaml")
        assert publisher.published == [
            (ICME_SAVED_TOPIC, {"path": "/spine/icon-mappings/icons.yaml"})
        ]

    def test_blank_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="path"):
            emit_icme_saved(_RecordingPublisher(), "")

    def test_topic_is_known_but_not_a_bar_render_topic(self) -> None:
        assert ICME_SAVED_TOPIC in KNOWN_TOPICS
        assert ICME_SAVED_TOPIC not in bs.BAR_TOPICS


class TestRulingC1NotADaemonTrigger:
    def test_daemon_and_converge_never_reference_icme(self) -> None:
        for func in (cli_main._run_daemon_run, cli_main._run_reactive_converge):
            assert "icme" not in inspect.getsource(func).lower()

    def test_icme_emitter_is_not_called_by_the_runtime(self) -> None:
        source = inspect.getsource(cli_main)
        assert "emit_icme_saved" not in source
