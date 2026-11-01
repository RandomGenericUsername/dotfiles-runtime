"""Shared palette-producer subprocess + artifact helpers.

Only the theme producer (``tpr``) uses these today. The module owns the
producer-agnostic mechanics so an adapter never re-implements them:

- a graceful-timeout subprocess runner (SIGINT first, SIGKILL fallback);
- the complete 8-artifact inspection + typed failure for a partial set;
- input/output validation and the entry builder that hashes the artifacts.

Nothing here knows a producer name; the caller passes it through so a typed
failure always names the tool that actually ran.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from runtime.adapters.hashing import (
    HASH_ALGORITHM,
    hash_file,
)
from runtime.domain.models import (
    PALETTE_ARTIFACT_NAMES,
    PaletteArtifacts,
    PaletteEntry,
    PaletteUnavailableError,
)

#: Internal producer timeout window (container teardown + generation).
PRODUCER_INTERNAL_TIMEOUT_SECONDS: Final[int] = 300

#: Extra time above the internal timeout for the producer's cleanup window.
PRODUCER_TIMEOUT_GRACE_SECONDS: Final[int] = 30

#: Production default: 330 s, deliberately above the 300 s internal timeout.
DEFAULT_TIMEOUT_SECONDS: Final[int] = (
    PRODUCER_INTERNAL_TIMEOUT_SECONDS + PRODUCER_TIMEOUT_GRACE_SECONDS
)

#: Grace after SIGINT (lets the producer's Python ``finally`` clean up)
#: before escalating to SIGKILL.
_TERMINATION_GRACE_SECONDS: Final[float] = 30.0

#: Pipe-drain bound after SIGKILL; a process that cannot be reaped is reported.
_REAP_TIMEOUT_SECONDS: Final[float] = 5.0


@dataclass(frozen=True, slots=True)
class ProducerRunResult:
    """Outcome of one producer subprocess, including timeout handling."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    elapsed_seconds: float
    termination: str | None = None


def _as_text(value: object, limit: int | None = None) -> str:
    """Decode subprocess output (text/bytes/None) without leaking ``b'...'``."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode(errors="replace")
    else:
        text = str(value)
    return text[:limit] if limit is not None else text


def _signal_process_group(proc: subprocess.Popen[str], sig: int) -> bool:
    """Signal the child's process group; False when it is already gone."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        # Fall back to the direct child only (e.g. getpgid/permission edge).
        try:
            proc.send_signal(sig)
            return True
        except (ProcessLookupError, OSError):
            return False


def _terminate_after_timeout(proc: subprocess.Popen[str]) -> tuple[str, str, str]:
    """Reap a timed-out child: SIGINT first (cleanup), then SIGKILL.

    SIGINT reaches the producer's main thread as ``KeyboardInterrupt``; its
    container runtime has a ``finally`` that force-removes the container. The
    grace period lets that cleanup finish. A child that ignores SIGINT is
    killed, so the adapter never hangs unbounded.
    """
    terminated = "exited"
    if proc.poll() is None and _signal_process_group(proc, signal.SIGINT):
        terminated = "SIGINT"
    try:
        stdout, stderr = proc.communicate(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_process_group(proc, signal.SIGKILL)
        if proc.poll() is None:
            proc.kill()
        terminated = "SIGKILL"
        try:
            stdout, stderr = proc.communicate(timeout=_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    return _as_text(stdout), _as_text(stderr), terminated


def run_producer(args: list[str], env: dict[str, str], timeout: int) -> ProducerRunResult:
    """Run a palette producer with a graceful timeout cleanup window.

    ``subprocess.run(timeout=...)`` SIGKILLs the child, so a containerized
    producer never runs its removal ``finally``. Using ``Popen`` + SIGINT gives
    it that chance; SIGKILL remains the bounded fallback.
    """
    started = time.monotonic()
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stdout, stderr, termination = _terminate_after_timeout(proc)
        return ProducerRunResult(
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
            elapsed_seconds=time.monotonic() - started,
            termination=termination,
        )
    return ProducerRunResult(
        returncode=proc.returncode,
        stdout=_as_text(stdout),
        stderr=_as_text(stderr),
        timed_out=False,
        elapsed_seconds=time.monotonic() - started,
    )


def inspect_artifacts(output_dir: Path) -> tuple[list[str], list[str]]:
    """Return ``(missing, not_files)`` for the complete palette artifact set."""
    missing: list[str] = []
    not_files: list[str] = []
    for name in PALETTE_ARTIFACT_NAMES:
        path = output_dir / name
        if path.is_file():
            continue
        if path.exists():
            not_files.append(name)
        else:
            missing.append(name)
    return missing, not_files


def artifact_failure(
    *,
    producer: str,
    wallpaper_path: Path,
    wallpaper_hash: str,
    artifact_dir: Path,
    missing: list[str],
    not_files: list[str],
    result: ProducerRunResult,
    hint: str,
) -> PaletteUnavailableError:
    """Build the typed failure for an exit-0 run with an incomplete artifact set.

    Carries the real return code, stdout/stderr, elapsed time, timeout/
    termination state and the missing/non-file artifact names. The message
    keeps the historical ``did not write expected artifact`` phrase so
    operators and existing log greps still find it.
    """
    names = missing + not_files
    detail_parts: list[str] = []
    if missing:
        detail_parts.append("missing=" + ",".join(missing))
    if not_files:
        detail_parts.append("not_files=" + ",".join(not_files))
    detail = "; ".join(detail_parts)
    name = names[0] if names else "<unknown>"
    message = f"{producer} did not write expected artifact: {artifact_dir / name}"
    if detail:
        message += f" ({detail})"
    if result.stdout:
        message += f" stdout:{result.stdout[:500]}"
    if result.stderr:
        message += f" stderr:{result.stderr[:2048]}"
    return PaletteUnavailableError(
        message,
        wallpaper_path=str(wallpaper_path),
        wallpaper_hash=wallpaper_hash,
        tool=producer,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr[:2048],
        elapsed_seconds=result.elapsed_seconds,
        timed_out=result.timed_out,
        termination=result.termination,
        missing_artifacts=tuple(names),
        hint=hint,
    )


def build_palette_entry(
    output_dir: Path,
    *,
    entry_hash: str,
    wallpaper_hash: str,
    template_set_hash: str,
    producer: str,
) -> PaletteEntry:
    """Hash the verified 8-artifact set and build the ``PaletteEntry``."""
    # Compute artifact hashes via binary chunked hash_file — wrap TOCTOU.
    try:
        artifact_for_entry: PaletteArtifacts = PaletteArtifacts(
            colors_yaml=hash_file(output_dir / "colors.yaml"),
            colors_conf=hash_file(output_dir / "colors.conf"),
            colors_gtk_css=hash_file(output_dir / "colors.gtk.css"),
            colors_adw_css=hash_file(output_dir / "colors.adw.css"),
            colors_sequences=hash_file(output_dir / "colors.sequences"),
            colors_rasi=hash_file(output_dir / "colors.rasi"),
            colors_kitty=hash_file(output_dir / "colors.kitty"),
            colors_btop=hash_file(output_dir / "colors.btop.theme"),
        )
    except (FileNotFoundError, PermissionError, IsADirectoryError, OSError) as exc:
        raise RuntimeError(f"cannot hash {producer} artifacts in {output_dir}: {exc}") from exc

    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    return PaletteEntry(
        hash_algorithm=HASH_ALGORITHM,
        kind="palette",
        entry_hash=entry_hash,
        source_wallpaper_hash=wallpaper_hash,
        input_template_hash=template_set_hash,
        artifact_hashes=artifact_for_entry,
        generated_at=generated_at,
    )


def validate_output_dir(output_dir: Path, expected_hash: str) -> None:
    """Reject degenerate, traversal, symlinked or mis-addressed output dirs."""
    if str(output_dir) in ("", "."):
        raise ValueError(f"output_dir must be a non-empty path, got {output_dir!r}")
    if ".." in output_dir.parts:
        raise ValueError(f"output_dir must not contain '..': {output_dir}")
    try:
        if output_dir.is_symlink():
            raise ValueError(f"output_dir must not be a symlink: {output_dir}")
        if output_dir.parent.is_symlink():
            raise ValueError(f"output_dir parent must not be a symlink: {output_dir.parent}")
    except OSError:
        # is_symlink may raise on permission; treat as invalid.
        pass
    if output_dir.name != expected_hash:
        raise ValueError(
            f"output_dir hash mismatch: expected {expected_hash}, got {output_dir.name}"
        )
    if len(str(output_dir)) > 4000:
        raise ValueError(f"output_dir path too long ({len(str(output_dir))} chars): {output_dir}")


def validate_wallpaper(wallpaper_path: Path) -> str:
    """Validate a wallpaper input and return its SHA-256 hex hash."""
    if str(wallpaper_path) in ("", "."):
        raise ValueError(f"wallpaper_path must be non-empty, got {wallpaper_path!r}")
    if not wallpaper_path.exists():
        raise FileNotFoundError(wallpaper_path)
    if wallpaper_path.is_dir():
        raise IsADirectoryError(wallpaper_path)
    if not wallpaper_path.is_file():
        # FIFO, socket, device, broken symlink, etc. — not a regular file.
        raise ValueError(f"wallpaper_path is not a regular file: {wallpaper_path}")
    # Size / type guard — avoid hashing /dev/zero, huge files, empty.
    try:
        st = wallpaper_path.stat()
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"wallpaper_path is not a regular file: {wallpaper_path}")
        if st.st_size == 0:
            raise ValueError(f"wallpaper_path is empty (0 bytes): {wallpaper_path}")
        # 100 MB cap — prevents DoS before the subprocess timeout.
        if st.st_size > 100 * 1024 * 1024:
            raise ValueError(f"wallpaper_path too large ({st.st_size} bytes): {wallpaper_path}")
    except OSError as exc:
        if isinstance(exc, (FileNotFoundError, IsADirectoryError, ValueError)):
            raise
        raise RuntimeError(f"cannot stat wallpaper_path {wallpaper_path}: {exc}") from exc

    try:
        return hash_file(wallpaper_path)
    except (PermissionError, OSError) as exc:
        raise RuntimeError(f"cannot hash wallpaper_path {wallpaper_path}: {exc}") from exc
