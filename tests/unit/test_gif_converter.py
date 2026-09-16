"""Unit tests for the GIF conversion stage (capture-backend-options, story 6).

No FFmpeg runs: the ``run`` callable is a fake recording its argv. Coverage:
scale math (75/50 halve-and-quarter geometry exactly, original unscaled),
command shape, loud typed failures (missing binary, non-zero exit,
timeout, transport error), and misuse rejection.
"""

from __future__ import annotations

import subprocess

import pytest

from runtime.adapters.gif_converter import (
    GIF_FPS_BAND,
    GifConversionError,
    build_gif_command,
    convert_to_gif,
    scale_filter,
    validate_gif_fps,
    validate_gif_size,
)


class _Ran:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stderr: str = "",
        exc: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self._returncode = returncode
        self._stderr = stderr
        self._exc = exc

    def __call__(self, command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = list(command)  # type: ignore[arg-type]
        self.calls.append((argv, dict(kwargs)))
        if self._exc is not None:
            raise self._exc
        return subprocess.CompletedProcess(argv, self._returncode, stdout="", stderr=self._stderr)


class TestScaleMath:
    def test_original_applies_no_filter(self) -> None:
        assert scale_filter("original") is None

    def test_50_halves_each_axis(self) -> None:
        assert scale_filter("50") == "scale=iw/2:ih/2:flags=lanczos"

    def test_75_scales_each_axis_exactly(self) -> None:
        assert scale_filter("75") == "scale=iw*3/4:ih*3/4:flags=lanczos"

    def test_unknown_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown GIF size"):
            scale_filter("25")
        with pytest.raises(ValueError, match="unknown GIF size"):
            validate_gif_size("bogus")


class TestFpsBand:
    def test_band_members_accepted(self) -> None:
        for fps in (10, 15, 20, 30):
            validate_gif_fps(fps)
        assert tuple(GIF_FPS_BAND) == (10, 15, 20, 30)

    def test_out_of_band_rejected(self) -> None:
        with pytest.raises(ValueError, match="frame rates"):
            validate_gif_fps(60)
        with pytest.raises(ValueError, match="frame rates"):
            validate_gif_fps(24)


class TestCommandShape:
    def test_original_converts_without_filter(self) -> None:
        assert build_gif_command("in.capture.mp4", "out.gif") == [
            "ffmpeg",
            "-y",
            "-i",
            "in.capture.mp4",
            "out.gif",
        ]

    def test_scaled_converts_with_filter(self) -> None:
        assert build_gif_command("in.capture.mp4", "out.gif", "50") == [
            "ffmpeg",
            "-y",
            "-i",
            "in.capture.mp4",
            "-vf",
            "scale=iw/2:ih/2:flags=lanczos",
            "out.gif",
        ]

    def test_unknown_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown GIF size"):
            build_gif_command("in.mp4", "out.gif", "25")


class TestConvert:
    def test_success_runs_expected_argv(self) -> None:
        ran = _Ran()
        convert_to_gif("in.capture.mp4", "out.gif", "50", run=ran, ffmpeg="ffmpeg")
        assert ran.calls == [
            (
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    "in.capture.mp4",
                    "-vf",
                    "scale=iw/2:ih/2:flags=lanczos",
                    "out.gif",
                ],
                {"capture_output": True, "text": True, "check": True, "timeout": 600.0},
            )
        ]

    def test_missing_binary_fails_loud_with_typed_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import runtime.adapters.gif_converter as converter

        monkeypatch.setattr(converter.shutil, "which", lambda _name: None)
        with pytest.raises(GifConversionError, match="ffmpeg not found"):
            convert_to_gif("in.capture.mp4", "out.gif", run=_Ran())
        assert GifConversionError("x").kind == "encoder_unavailable"

    def test_nonzero_exit_surfaces_stderr_tail_as_typed_error(self) -> None:
        ran = _Ran(
            exc=subprocess.CalledProcessError(1, ["ffmpeg"], stderr="  boom: oversized region\n")
        )
        with pytest.raises(GifConversionError, match="exit 1.*oversized region"):
            convert_to_gif("in.capture.mp4", "out.gif", run=ran, ffmpeg="ffmpeg")

    def test_timeout_is_typed(self) -> None:
        ran = _Ran(exc=subprocess.TimeoutExpired(["ffmpeg"], 600.0))
        with pytest.raises(GifConversionError, match="timed out"):
            convert_to_gif("in.capture.mp4", "out.gif", run=ran, ffmpeg="ffmpeg")

    def test_transport_failure_is_typed(self) -> None:
        ran = _Ran(exc=OSError("permission denied"))
        with pytest.raises(GifConversionError, match="could not run"):
            convert_to_gif("in.capture.mp4", "out.gif", run=ran, ffmpeg="ffmpeg")

    def test_unknown_size_rejected_before_running(self) -> None:
        ran = _Ran()
        with pytest.raises(ValueError, match="unknown GIF size"):
            convert_to_gif("in.capture.mp4", "out.gif", "25", run=ran, ffmpeg="ffmpeg")
        assert ran.calls == []
