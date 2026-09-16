"""GIF conversion — the GIF special pipeline's FFmpeg stage (plan §43).

The launcher captures video at the selected GIF frame rate into an
intermediate file; the resident capture host runs this stage after the
recorder stops, converting the intermediate to the final GIF at the
requested scale. Audio hardware is never involved (plan §8: GIF has no
audio track — the launcher enforces that before the recorder spawns).

Failure modes surface as the typed :class:`GifConversionError` (plan §49
``encoder_unavailable``) rather than leaking ``subprocess`` internals;
misuse (unknown size) is a ``ValueError``, matching the existing
validation style. The ``run`` callable is injectable so the stage is
unit-testable without FFmpeg.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction

__all__ = [
    "DEFAULT_GIF_CONVERT_TIMEOUT_S",
    "GIF_FPS_BAND",
    "GIF_SIZE_SCALES",
    "GifConversionError",
    "build_gif_command",
    "convert_to_gif",
    "scale_filter",
    "validate_gif_fps",
    "validate_gif_size",
]

#: Frame rates the GIF special pipeline captures at (plan §8/§43).
GIF_FPS_BAND: tuple[int, ...] = (10, 15, 20, 30)

#: Requested scale → axis multiplier (``None`` keeps the capture geometry).
GIF_SIZE_SCALES: Mapping[str, float | None] = {
    "original": None,
    "75": 0.75,
    "50": 0.5,
}

#: Wall-clock ceiling for one FFmpeg conversion (seconds).
DEFAULT_GIF_CONVERT_TIMEOUT_S = 600.0


class GifConversionError(RuntimeError):
    """Typed GIF-pipeline failure (plan §49 ``encoder_unavailable``).

    Carries the UI-facing category as :attr:`kind` so the existing
    typed-error channel (``ErrorView(kind, message)`` / launcher
    ``error_kind`` vocabulary) can categorize it.
    """

    kind = "encoder_unavailable"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.kind = "encoder_unavailable"


def validate_gif_size(size: str) -> None:
    """Reject an unknown GIF scale loudly (never silently unscaled)."""
    if size not in GIF_SIZE_SCALES:
        raise ValueError(f"unknown GIF size {size!r}; known: {sorted(GIF_SIZE_SCALES)}")


def validate_gif_fps(fps: int) -> None:
    """Reject a frame rate outside the GIF capture band loudly."""
    if fps not in GIF_FPS_BAND:
        raise ValueError(f"GIF mode supports frame rates {list(GIF_FPS_BAND)}, got {fps!r}")


def scale_filter(size: str) -> str | None:
    """Return the FFmpeg ``scale`` filter for ``size`` (``None`` = original).

    Ratios are expressed as exact integer fractions (``iw*3/4``, ``iw/2``)
    so the output dimensions are precisely the scaled capture geometry.
    """
    validate_gif_size(size)
    scale = GIF_SIZE_SCALES[size]
    if scale is None:
        return None
    ratio = Fraction(scale).limit_denominator(100)

    def _axis(var: str) -> str:
        if ratio.numerator == 1:
            return f"{var}/{ratio.denominator}"
        return f"{var}*{ratio.numerator}/{ratio.denominator}"

    return f"scale={_axis('iw')}:{_axis('ih')}:flags=lanczos"


def build_gif_command(
    input_path: str,
    output_path: str,
    size: str = "original",
    *,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Build the FFmpeg argv converting the intermediate to the final GIF."""
    command = [ffmpeg, "-y", "-i", input_path]
    video_filter = scale_filter(size)
    if video_filter is not None:
        command.extend(["-vf", video_filter])
    command.append(output_path)
    return command


def convert_to_gif(
    input_path: str,
    output_path: str,
    size: str = "original",
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ffmpeg: str | None = None,
    timeout: float = DEFAULT_GIF_CONVERT_TIMEOUT_S,
) -> None:
    """Convert the intermediate capture to the final GIF (raise loudly).

    A missing FFmpeg binary, a non-zero exit, or a timeout raises
    :class:`GifConversionError`; the intermediate is left in place on
    failure for debugging (the caller removes it on success).
    """
    validate_gif_size(size)
    binary = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
    if not binary:
        raise GifConversionError("ffmpeg not found on PATH; cannot convert GIF")
    command: Sequence[str] = build_gif_command(input_path, output_path, size, ffmpeg=binary)
    try:
        run(command, capture_output=True, text=True, check=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise GifConversionError(f"ffmpeg could not run: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GifConversionError(f"GIF conversion timed out after {timeout:g}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = ((exc.stderr or exc.stdout) or "").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise GifConversionError(f"GIF conversion failed (exit {exc.returncode}{suffix})") from exc
    except OSError as exc:
        raise GifConversionError(f"GIF conversion could not run: {exc}") from exc
