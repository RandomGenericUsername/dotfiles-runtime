"""Segmenting recorder — survives an unexpected backend death (AD-37).

The recorder child (``gpu-screen-recorder`` / ``wf-recorder``) can die
without an intentional stop: a compositor output reset, a KMS/portal
hiccup, or memory pressure from a concurrent wallpaper derivation. The
capture job's timer is monotonic and decoupled from the child, so a dead
backend previously meant a truncated file with a still-counting timer.

This adapter makes the captured stream resilient without touching the
controller's state machine: it owns a *sequence* of short-lived
:class:`~runtime.adapters.subprocess_recorder.SubprocessRecorder`
children, each writing a numbered segment next to the promised output
path. On the host's cadence tick, :meth:`ensure_alive` restarts the child
on a fresh segment when the previous one died unintentionally, so the gap
is only the restart window. On :meth:`stop` the segments are joined into
the promised output path with FFmpeg's concat demuxer (stream copy, with a
re-encode fallback), and the segment files are removed on success.

The promised output path, pause/resume signalling, and the finalize
notification contract are unchanged: a run that never restarts behaves
exactly like a single :class:`SubprocessRecorder`, and a run that does
still ends with one playable file at the promised path.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path

from runtime.adapters.subprocess_recorder import SubprocessRecorder, _infer_backend
from runtime.ports.jobs import IRecorderProcess

__all__ = [
    "DEFAULT_MAX_RESTARTS",
    "DEFAULT_RESTART_ATTEMPT_INTERVAL_S",
    "DEFAULT_RESTART_GRACE_S",
    "DEFAULT_STABLE_S",
    "SegmentingRecorder",
    "replace_output",
    "segment_path",
]

logger = logging.getLogger(__name__)

#: Hard cap on restart attempts per job (backstop against a spawn storm).
DEFAULT_MAX_RESTARTS = 60

#: How long a restart may keep being attempted before the death is fatal.
#: A transient outage (a compositor reset, or memory pressure from a
#: concurrent wallpaper derivation that takes ~10s) must not be treated as
#: fatal on the first failed spawn — the job keeps trying within this window.
DEFAULT_RESTART_GRACE_S = 30.0

#: Minimum spacing between restart attempts (avoids a spawn storm while the
#: backend is momentarily unable to start).
DEFAULT_RESTART_ATTEMPT_INTERVAL_S = 0.5

#: A child alive this long counts as recovered: the grace window resets, so a
#: later, unrelated outage gets its own full window.
DEFAULT_STABLE_S = 2.0

#: Concat callable: ``(segments, output) -> None`` (raises on failure).
IConcat = Callable[[Sequence[Path], Path], None]


def segment_path(output: str, index: int) -> Path:
    """Numbered segment path beside the promised output (same extension)."""
    path = Path(output)
    return path.with_name(f"{path.stem}.part{index:02d}{path.suffix}")


def _output_flag(backend: str) -> str:
    """The recorder argv flag that carries the output file path."""
    return "-o" if backend == "gpu-screen-recorder" else "-f"


def replace_output(command: Sequence[str], backend: str, output: str) -> list[str]:
    """Return ``command`` with its output path swapped for ``output``.

    ``gpu-screen-recorder`` carries it on ``-o`` and ``wf-recorder`` on
    ``-f``; both are single-value flags, so only the token after the flag is
    replaced. A command without the flag is returned unchanged (the recorder
    would then write to its own default — a surfaced startup failure, never a
    silent overwrite of the promised path).
    """
    flag = _output_flag(backend)
    tokens = list(command)
    for index, token in enumerate(tokens):
        if token == flag and index + 1 < len(tokens):
            tokens[index + 1] = output
            return tokens
    logger.warning(
        "segmenting recorder: no %s flag in recorder command; output path not remapped",
        flag,
    )
    return tokens


class SegmentingRecorder(IRecorderProcess):
    """An :class:`IRecorderProcess` that restarts the child on unexpected death.

    Args:
        output_path: the promised final path (the only file the caller sees).
        command: the recorder argv for the FIRST segment; later segments are
            derived by replacing the output flag value.
        backend: recorder backend name (selects the output flag).
        recorder_factory: ``(command) -> IRecorderProcess``; defaults to
            :class:`SubprocessRecorder` with the same backend.
        concat: ``(segments, output) -> None``; defaults to the FFmpeg concat
            adapter. Injected for hermetic tests.
        max_restarts: restart attempts before the death is fatal.
    """

    def __init__(
        self,
        output_path: str,
        command: Sequence[str],
        *,
        backend: str | None = None,
        recorder_factory: Callable[[Sequence[str]], IRecorderProcess] | None = None,
        concat: IConcat | None = None,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        grace: float = DEFAULT_RESTART_GRACE_S,
        attempt_interval: float = DEFAULT_RESTART_ATTEMPT_INTERVAL_S,
        stable_s: float = DEFAULT_STABLE_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not command:
            raise ValueError("segmenting recorder command must be non-empty")
        import time

        self._output = Path(output_path)
        self._command = list(command)
        self._backend = backend or _infer_backend(self._command)
        self._recorder_factory = recorder_factory
        self._concat = concat
        self._max_restarts = max_restarts
        self._grace = grace
        self._attempt_interval = attempt_interval
        self._stable_s = stable_s
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._segments: list[Path] = []
        self._current: IRecorderProcess | None = None
        self._index = 0
        self._restarts = 0
        self._stopped = False
        self._paused = False
        self._concat_ok: bool | None = None
        self._down_since: float | None = None
        self._next_attempt = 0.0
        self._segment_started_at: float | None = None

    # ── Observability ────────────────────────────────────────────────

    @property
    def segments(self) -> list[Path]:
        """Segments produced so far (in order)."""
        return list(self._segments)

    @property
    def restarts(self) -> int:
        """How many times the child was restarted after an unexpected death."""
        return self._restarts

    @property
    def concat_ok(self) -> bool | None:
        """``None`` before stop, else whether the segments were joined."""
        return self._concat_ok

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._current is not None:
            raise RuntimeError("segmenting recorder already started")
        self._start_segment()

    def stop(self) -> None:
        """Cancel any pending restart, finalize the child, and join segments."""
        if self._stopped:
            return
        self._stopped = True
        current = self._current
        self._current = None
        if current is not None:
            current.stop()
        self._finalize()

    def pause(self) -> None:
        self._paused = True
        self._delegate().pause()

    def resume(self) -> None:
        self._paused = False
        self._delegate().resume()

    def is_running(self) -> bool:
        return self._current is not None and self._current.is_running()

    def ensure_alive(self) -> bool:
        """Restart the child onto a new segment if it died unexpectedly.

        A transient outage is retried within a grace window: a failed spawn
        (the backend briefly unable to start during a compositor reset or a
        concurrent derivation) keeps returning ``True`` and is retried on the
        next tick, rather than being declared fatal on the first attempt.
        ``False`` is returned only when the job is stopped, the grace window
        elapses, or the hard attempt cap is hit — a persistent failure the
        host must report honestly.
        """
        if self._stopped:
            return True
        now = self._clock()
        if self._current is not None and self._current.is_running():
            # Recovered: a child that stays up long enough resets the grace
            # so a later, unrelated outage gets its own full window.
            if (
                self._segment_started_at is not None
                and now - self._segment_started_at >= self._stable_s
            ):
                self._down_since = None
            return True
        if self._down_since is None:
            self._down_since = now
            status = self._current.exit_status() if self._current is not None else None
            logger.warning(
                "segmenting recorder: child exited unexpectedly (status %s); "
                "restarting within %.0fs grace",
                status,
                self._grace,
            )
        if now - self._down_since > self._grace:
            logger.warning(
                "segmenting recorder: restart grace (%.0fs) elapsed; giving up",
                self._grace,
            )
            return False
        if self._restarts >= self._max_restarts:
            logger.warning(
                "segmenting recorder: restart cap (%d) reached; giving up",
                self._max_restarts,
            )
            return False
        if now < self._next_attempt:
            return True
        self._next_attempt = now + self._attempt_interval
        self._restarts += 1
        try:
            self._start_segment()
        except Exception:
            logger.exception("segmenting recorder: restart failed; will retry")
            return True
        return True

    def exit_status(self) -> int | None:
        return self._current.exit_status() if self._current is not None else None

    # ── Internals ────────────────────────────────────────────────────

    def _start_segment(self) -> None:
        path = segment_path(str(self._output), self._index)
        self._index += 1
        command = replace_output(self._command, self._backend, str(path))
        recorder = (
            self._recorder_factory(command)
            if self._recorder_factory is not None
            else SubprocessRecorder(command, backend=self._backend)
        )
        recorder.start()
        if self._paused:
            recorder.pause()
        self._segments.append(path)
        self._current = recorder
        self._segment_started_at = self._clock()

    def _delegate(self) -> IRecorderProcess:
        if self._current is None:
            raise RuntimeError("segmenting recorder has no live child")
        return self._current

    def _finalize(self) -> None:
        """Join the produced segments into the promised output path."""
        live = [path for path in self._segments if path.exists()]
        if not live:
            self._concat_ok = False
            logger.warning("segmenting recorder: no segment files to finalize")
            return
        if len(live) == 1:
            # The healthy single-segment path (no restart): a plain rename is
            # zero-dependency and instant. Validate first on the real path so a
            # crashed backend's unreadable container is never passed off as a
            # finished recording (an injected concat keeps the rename contract).
            if self._concat is None:
                import shutil

                probe = shutil.which("ffprobe")
                if probe is not None and _probe_duration(probe, live[0]) is None:
                    logger.warning(
                        "segmenting recorder: single segment is unreadable; keeping %s",
                        live[0],
                    )
                    self._concat_ok = False
                    return
            try:
                os.replace(live[0], self._output)
            except OSError:
                logger.exception("segmenting recorder: could not rename single segment")
                self._concat_ok = False
                return
            self._concat_ok = True
            return
        concat = self._concat if self._concat is not None else _ffmpeg_concat
        try:
            concat(live, self._output)
        except Exception:
            logger.exception(
                "segmenting recorder: concat failed; %d segment(s) kept", len(live)
            )
            self._concat_ok = False
            return
        self._concat_ok = True
        for path in live:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("segmenting recorder: could not remove segment %s", path)


def _probe_duration(binary: str | None, path: Path) -> float | None:
    """Readable duration of ``path`` via ffprobe, or ``None`` if unreadable.

    A segment whose backend was SIGKILLed (or crashed before flushing its
    container) has no index and cannot be joined; probing it lets the join
    drop that segment instead of silently truncating the whole recording.
    """
    import subprocess

    if not binary:
        return None
    try:
        result = subprocess.run(
            [
                binary, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _ffmpeg_concat(segments: Sequence[Path], output: Path) -> None:
    """Default concat via FFmpeg's concat demuxer (stream copy, then re-encode).

    Unreadable segments (a crashed backend that never flushed its container)
    are dropped with a warning so one bad segment cannot truncate the whole
    recording. Stream copy preserves the captured quality and is near-instant;
    when the segments' parameters differ (a backend restart can change the
    encoder profile) or the copy comes out short, a lossless-ish H.264
    re-encode is used so the promised output still exists. A total failure
    propagates to the caller (which keeps the segments on disk).
    """
    import shutil
    import subprocess

    binary = shutil.which("ffmpeg")
    if not binary:
        raise RuntimeError("ffmpeg not found on PATH; cannot concatenate segments")
    probe = shutil.which("ffprobe")
    durations = [(path, _probe_duration(probe, path)) for path in segments]
    readable = [path for path, duration in durations if duration is not None]
    dropped = [path for path, duration in durations if duration is None]
    if dropped:
        logger.warning(
            "segmenting recorder: dropping %d unreadable segment(s): %s",
            len(dropped),
            ", ".join(path.name for path in dropped),
        )
    if not readable:
        raise RuntimeError("no readable segment to concatenate")
    if len(readable) == 1:
        shutil.copyfile(readable[0], output)
        return

    listing = output.with_name(f"{output.stem}.concat.txt")
    listing.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in readable), encoding="utf-8"
    )
    expected = sum(
        duration for path, duration in durations if duration is not None
    )

    def _run(args: list[str]) -> None:
        subprocess.run(
            [binary, "-y", *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=600,
        )

    try:
        _run(
            [
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c", "copy", "-fflags", "+genpts", str(output),
            ]
        )
        actual = _probe_duration(probe, output)
        if actual is None or actual < expected * 0.7:
            raise RuntimeError(
                f"stream-copy concat too short ({actual!r} vs ~{expected:.1f}s)"
            )
    except Exception:
        logger.warning("segmenting recorder: stream-copy concat failed; re-encoding")
        _run(
            [
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                str(output),
            ]
        )
    finally:
        try:
            listing.unlink(missing_ok=True)
        except OSError:
            pass
