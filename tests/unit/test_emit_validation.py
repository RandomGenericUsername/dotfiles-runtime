"""Unit tests for P5-1-2b-ii-a Emit validation (no live bus).

Schema/size/depth/sv-compatibility per the 2a Gate-1 rulings plus the
sliding-window rate limiter with a fake clock. The embedded schema table
↔ contract topics pin lives in `test_dbus_conformance.py`.
"""

from __future__ import annotations

import pytest

from runtime.adapters.emit_validation import (
    MAX_PAYLOAD_BYTES,
    MAX_PAYLOAD_DEPTH,
    PER_TOPIC_RATE_PER_MIN,
    RATE_GLOBAL_PER_MIN,
    RATE_PER_KEY_PER_MIN,
    RATE_WINDOW_S,
    EmitRateLimiter,
    EmitValidator,
    payload_depth,
    payload_size_bytes,
)
from runtime.domain.models import PayloadTooLarge, RateLimited, UnknownTopic


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _validator(clock: _Clock | None = None) -> tuple[EmitValidator, _Clock]:
    clock = clock if clock is not None else _Clock()
    return EmitValidator(clock=clock), clock


def _capture_payload() -> dict[str, object]:
    return {"state": "recording", "elapsed_seconds": 7}


class TestConstants:
    def test_cap_numbers_are_the_ruled_values(self) -> None:
        assert MAX_PAYLOAD_BYTES == 64 * 1024
        assert MAX_PAYLOAD_DEPTH == 8
        assert RATE_PER_KEY_PER_MIN == 60
        assert RATE_GLOBAL_PER_MIN == 600
        assert RATE_WINDOW_S == 60.0

    def test_capture_state_override_is_derived_and_below_global_backstop(self) -> None:
        """The capture.state budget is a code constant: 4× the contract
        cadence (240/min), strictly above the base cap and strictly below the
        hub-wide ceiling so the global backstop still bites."""
        budget = PER_TOPIC_RATE_PER_MIN["capture.state"]
        assert budget == RATE_PER_KEY_PER_MIN * 4 == 240
        assert RATE_PER_KEY_PER_MIN < budget < RATE_GLOBAL_PER_MIN

    def test_overrides_only_cover_contract_high_cadence_topics(self) -> None:
        assert set(PER_TOPIC_RATE_PER_MIN) == {"capture.state"}


class TestKnownTopics:
    def test_unknown_topic_rejected(self) -> None:
        validator, _ = _validator()
        with pytest.raises(UnknownTopic):
            validator.validate("nope.topic", {"a": "b"}, ":1.1")
        with pytest.raises(UnknownTopic):
            validator.validate("", {"a": "b"}, ":1.1")


class TestSchemas:
    def test_happy_paths_per_topic(self) -> None:
        validator, _ = _validator()
        validator.validate("icme.saved", {"path": "/a/b"}, ":1.1")
        validator.validate("capture.state", _capture_payload(), ":1.1")
        validator.validate(
            "speedtest.finished",
            {"down_mbps": 1.5, "up_mbps": 2.5, "latency_ms": 3.5},
            ":1.1",
        )

    def test_missing_required_rejected(self) -> None:
        validator, _ = _validator()
        with pytest.raises(PayloadTooLarge):
            validator.validate("icme.saved", {}, ":1.1")

    def test_wrong_type_rejected(self) -> None:
        validator, _ = _validator()
        with pytest.raises(PayloadTooLarge):
            validator.validate("icme.saved", {"path": 123}, ":1.1")

    def test_enum_violation_rejected(self) -> None:
        validator, _ = _validator()
        with pytest.raises(PayloadTooLarge):
            validator.validate("capture.state", {"state": "bogus", "elapsed_seconds": 1}, ":1.1")
        for ok in ("idle", "recording", "paused"):
            validator.validate("capture.state", {"state": ok, "elapsed_seconds": 1}, ":1.1")

    def test_additive_unknown_fields_accepted(self) -> None:
        """AD-34: additive optional fields are non-breaking — the hub
        must not reject future fields."""
        validator, _ = _validator()
        validator.validate("icme.saved", {"path": "/a", "future": "x"}, ":1.1")

    def test_reserved_members_rejected(self) -> None:
        validator, _ = _validator()
        with pytest.raises(PayloadTooLarge, match="_epoch"):
            validator.validate("icme.saved", {"path": "/a", "_epoch": 1}, ":1.1")
        with pytest.raises(PayloadTooLarge, match="_seq"):
            validator.validate("icme.saved", {"path": "/a", "_seq": 1}, ":1.1")


class TestSizeDepth:
    def test_oversize_rejected_with_size(self) -> None:
        validator, _ = _validator()
        with pytest.raises(PayloadTooLarge) as excinfo:
            validator.validate("icme.saved", {"path": "x" * (64 * 1024)}, ":1.1")
        assert excinfo.value.size > 64 * 1024

    def test_over_depth_rejected(self) -> None:
        def _nest(levels: int) -> object:
            value: object = "leaf"
            for _ in range(levels):
                value = {"nest": value}
            return value

        validator, _ = _validator()
        with pytest.raises(PayloadTooLarge, match="depth"):
            validator.validate("icme.saved", {"path": "x", "deep": _nest(9)}, ":1.1")
        validator.validate("icme.saved", {"path": "x", "deep": _nest(6)}, ":1.1")

    def test_depth_counts_top_dict_as_one(self) -> None:
        assert payload_depth({"a": "b"}) == 1
        assert payload_depth("scalar") == 0
        assert payload_depth({"a": {"b": {"c": 1}}}) == 3
        assert payload_depth({"a": [1, 2]}) == 2

    def test_size_is_canonical(self) -> None:
        assert payload_size_bytes({"b": 1, "a": 2}) == payload_size_bytes({"a": 2, "b": 1})


class TestSvCompatibility:
    @pytest.mark.parametrize(
        "value",
        [b"bytes", None, (), {"k": b"bytes"}, {"k": None}, {"k": []}, {"k": [[]]}, 2**63],
    )
    def test_unrepresentable_rejected(self, value: object) -> None:
        validator, _ = _validator()
        with pytest.raises(PayloadTooLarge):
            validator.validate("icme.saved", {"path": "x", "v": value}, ":1.1")

    def test_non_finite_float_rejected(self) -> None:
        validator, _ = _validator()
        with pytest.raises(PayloadTooLarge):
            validator.validate(
                "speedtest.finished",
                {"down_mbps": float("nan"), "up_mbps": 1.0, "latency_ms": 1.0},
                ":1.1",
            )
        with pytest.raises(PayloadTooLarge):
            validator.validate(
                "speedtest.finished",
                {"down_mbps": float("inf"), "up_mbps": 1.0, "latency_ms": 1.0},
                ":1.1",
            )

    def test_non_string_key_rejected(self) -> None:
        validator, _ = _validator()
        with pytest.raises(PayloadTooLarge):
            validator.validate("icme.saved", {"path": "x", 1: "y"}, ":1.1")  # type: ignore[dict-item]


class TestRateLimiter:
    def test_sixty_first_per_key_then_limited(self) -> None:
        """A topic without an override keeps the base 60/min cap."""
        limiter = EmitRateLimiter()
        for _ in range(60):
            limiter.check(":1.1", "icme.saved")
        with pytest.raises(RateLimited):
            limiter.check(":1.1", "icme.saved")

    def test_window_slides(self) -> None:
        clock = _Clock()
        limiter = EmitRateLimiter(clock=clock)
        for _ in range(60):
            limiter.check(":1.1", "icme.saved")
        clock.advance(60.0)
        limiter.check(":1.1", "icme.saved")

    def test_per_sender_topic_isolation(self) -> None:
        limiter = EmitRateLimiter()
        for _ in range(60):
            limiter.check(":1.1", "icme.saved")
        limiter.check(":1.2", "icme.saved")  # other sender unaffected
        limiter.check(":1.1", "capture.state")  # other topic unaffected

    def test_global_ceiling(self) -> None:
        limiter = EmitRateLimiter(per_key=1000, global_limit=3)
        limiter.check(":1.1", "capture.state")
        limiter.check(":1.2", "capture.state")
        limiter.check(":1.3", "icme.saved")
        with pytest.raises(RateLimited):
            limiter.check(":1.4", "icme.saved")

    def test_validator_counts_accepted_only(self) -> None:
        """Structural failures do not consume quota (validate-then-count)."""
        clock = _Clock()
        validator, _ = _validator(clock)
        for _ in range(60):
            validator.validate("icme.saved", {"path": "/a"}, ":1.9")
        with pytest.raises(PayloadTooLarge):
            validator.validate("icme.saved", {}, ":1.9")  # invalid: no quota spent
        with pytest.raises(RateLimited):
            validator.validate("icme.saved", {"path": "/a"}, ":1.9")  # 61st valid: spent


class TestCaptureStateBudget:
    """The per-topic override keeps the >= 1/s recording stream alive."""

    def test_limit_is_override_not_base(self) -> None:
        limiter = EmitRateLimiter()
        assert limiter.per_key_limit("capture.state") == 240
        assert limiter.per_key_limit("icme.saved") == 60

    def test_sustained_recording_with_transitions_is_not_throttled(self) -> None:
        """5 minutes of 1/s ticks plus periodic transitions stay under budget.

        The base 60/min cap throttles this same stream, so the test also
        proves the override is load-bearing rather than a wider safety margin.
        """
        clock = _Clock()
        limiter = EmitRateLimiter(clock=clock)
        base = EmitRateLimiter(clock=clock, per_topic={})  # override disabled
        base_throttled = False
        for second in range(300):
            limiter.check(":1.1", "capture.state")
            try:
                base.check(":1.1", "capture.state")
            except RateLimited:
                base_throttled = True
            if second % 30 == 0:  # a transition burst every 30 s
                limiter.check(":1.1", "capture.state")
                try:
                    base.check(":1.1", "capture.state")
                except RateLimited:
                    base_throttled = True
            clock.advance(1.0)
        assert base_throttled, "the base cap must be the thing the override fixes"

    def test_transition_burst_within_cadence_is_accepted(self) -> None:
        """Base cap would throttle the 61st emit in a window; override does not."""
        clock = _Clock()
        limiter = EmitRateLimiter(clock=clock)
        base = EmitRateLimiter(clock=clock, per_topic={})
        for _ in range(60):
            limiter.check(":1.1", "capture.state")  # 60 contract ticks
            base.check(":1.1", "capture.state")
        for _ in range(4):  # start/pause/resume/stop transitions
            limiter.check(":1.1", "capture.state")
        with pytest.raises(RateLimited):
            base.check(":1.1", "capture.state")  # the base cap is tripped

    def test_override_boundary_still_bounded(self) -> None:
        limiter = EmitRateLimiter()
        for _ in range(240):
            limiter.check(":1.1", "capture.state")
        with pytest.raises(RateLimited):
            limiter.check(":1.1", "capture.state")

    def test_global_backstop_still_applies_to_capture_state(self) -> None:
        limiter = EmitRateLimiter(global_limit=3)
        limiter.check(":1.1", "capture.state")
        limiter.check(":1.2", "capture.state")
        limiter.check(":1.3", "capture.state")
        with pytest.raises(RateLimited):
            limiter.check(":1.4", "capture.state")

    def test_injected_override_never_lowers_below_base(self) -> None:
        """A table entry only raises the floor; ``per_key`` is preserved."""
        limiter = EmitRateLimiter(per_topic={"capture.state": 5})
        assert limiter.per_key_limit("capture.state") == 60

    def test_validator_accepts_multi_minute_capture_state_stream(self) -> None:
        """End-to-end through EmitValidator: 3 minutes at 1/s + transitions.

        Without the per-topic override the transition emits inside the 60 s
        window would make the 61st validate raise :class:`RateLimited`.
        """
        clock = _Clock()
        validator, _ = _validator(clock)
        for tick in range(180):
            validator.validate(
                "capture.state",
                {"state": "recording", "elapsed_seconds": tick},
                ":1.1",
            )
            if tick % 30 == 0:  # pause/resume burst
                validator.validate(
                    "capture.state",
                    {"state": "paused", "elapsed_seconds": tick},
                    ":1.1",
                )
            clock.advance(1.0)
