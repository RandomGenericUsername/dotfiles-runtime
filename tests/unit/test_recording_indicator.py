"""Recording-indicator harness + no-polling invariant (Phase 5, 5-4, AD-40).

The Python harness stands in for the AGS widget (no JS test runner). It
consumes pushed ``capture.state`` through the ``IEventSubscriber`` port,
never reads a status file, and interpolates only from a monotonic clock.
An AST/text scan pins the no-polling invariant on both the Python harness
and the GJS bar widget (the poll loop was removed).
"""

from __future__ import annotations

import ast
import os
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from runtime.application.indicator import IndicatorView, RecordingIndicator
from runtime.ports.event_bus import IEventSubscriber

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PY_HARNESS = _REPO_ROOT / "src" / "runtime" / "application" / "indicator.py"
#: The bar widget is owned by `ags-apps`, not this repo. The invariant is still
#: worth checking when a checkout is available; CI skips it with this reason.
_BAR_WIDGET_ENV = "DOTFILES_AGS_BAR_WIDGET"


def _bar_widget() -> Path | None:
    raw = os.environ.get(_BAR_WIDGET_ENV)
    candidates = [Path(raw).expanduser()] if raw else []
    candidates.append(
        _REPO_ROOT.parent / "ags-apps" / "bar" / "widgets" / "recording.tsx"
    )
    return next((path for path in candidates if path.is_file()), None)


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeSubscriber(IEventSubscriber):
    def __init__(self) -> None:
        self.handlers: dict[str, list[Callable[[str, Mapping[str, object]], None]]] = {}

    def subscribe(
        self, topic: str, handler: Callable[[str, Mapping[str, object]], None]
    ) -> None:
        self.handlers.setdefault(topic, []).append(handler)

    def emit(self, topic: str, payload: Mapping[str, object]) -> None:
        for handler in self.handlers.get(topic, []):
            handler(topic, payload)


def _indicator() -> tuple[RecordingIndicator, _FakeSubscriber, _Clock]:
    subscriber = _FakeSubscriber()
    clock = _Clock()
    indicator = RecordingIndicator(subscriber, clock=clock)
    return indicator, subscriber, clock


class TestRecordingIndicator:
    def test_subscribes_only_to_capture_state(self) -> None:
        _, subscriber, _ = _indicator()
        assert set(subscriber.handlers) == {"capture.state"}

    def test_renders_pushed_state_and_elapsed(self) -> None:
        indicator, subscriber, _ = _indicator()
        subscriber.emit("capture.state", {"state": "recording", "elapsed_seconds": 5})
        assert indicator.snapshot() == IndicatorView("recording", 5, visible=True)

    def test_interpolates_between_pushes_only_while_recording(self) -> None:
        indicator, subscriber, clock = _indicator()
        subscriber.emit("capture.state", {"state": "recording", "elapsed_seconds": 5})
        clock.advance(3.2)
        assert indicator.snapshot().elapsed_seconds == 8

    def test_paused_does_not_interpolate(self) -> None:
        indicator, subscriber, clock = _indicator()
        subscriber.emit("capture.state", {"state": "paused", "elapsed_seconds": 9})
        clock.advance(30.0)
        assert indicator.snapshot() == IndicatorView("paused", 9, visible=True)

    def test_idle_is_hidden(self) -> None:
        indicator, subscriber, _ = _indicator()
        subscriber.emit("capture.state", {"state": "idle", "elapsed_seconds": 0})
        view = indicator.snapshot()
        assert view.state == "idle" and view.visible is False

    def test_malformed_events_are_ignored(self) -> None:
        indicator, subscriber, _ = _indicator()
        subscriber.emit("capture.state", {"state": "bogus", "elapsed_seconds": 4})
        assert indicator.snapshot().state == "idle"
        subscriber.emit("capture.state", {"state": "recording", "elapsed_seconds": "x"})
        assert indicator.snapshot().elapsed_seconds == 0


class TestNoPollingInvariant:
    def test_python_harness_reads_no_state_file(self) -> None:
        tree = ast.parse(_PY_HARNESS.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported & {"json", "pathlib", "subprocess", "os", "shutil"} == set()
        source = _PY_HARNESS.read_text(encoding="utf-8")
        assert "capture-tool" not in source
        assert "state.json" not in source
        assert "status.json" not in source

    def test_bar_widget_has_no_poll_loop(self) -> None:
        widget = _bar_widget()
        if widget is None:
            pytest.skip(
                f"bar widget not available; set {_BAR_WIDGET_ENV} to a "
                "ags-apps/bar/widgets/recording.tsx checkout to run this check"
            )
        source = widget.read_text(encoding="utf-8")
        # The old indicator polled `capture-tool status` every 500ms.
        assert "spawn_command_line_sync" not in source
        assert 'execAsync(["capture-tool"' not in source
        assert "readStatus" not in source
        # The new indicator subscribes to the contract domain event.
        assert "capture.state" in source
        assert "subscribe" in source
        assert "Control" in source
