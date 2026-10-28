"""CSG adapter with env override (Story 1.7, AD-7).

Directs ``csg generate`` output via ``COLORSCHEME__OUTPUT__DIRECTORY``
env override into ``cache/palettes/<ph>/`` without editing
provisioning's ``settings.toml``.

Container forwarding (AD-7 last sentence):
``csg`` runs in container mode (podman/docker via ``oci-runtime``):
the output-dir override must be passed **INTO** the container environment,
not just the host process. This adapter only sets the host env dict and
passes it to ``subprocess.run(env=)`` — ``csg``'s own
``container_processor`` forwards the env into ``RunConfig`` via
``oci_runtime.ContainerRuntimePort``. Verified by
``tests/integration/test_csg_determinism`` zero ``-o`` flag pattern
where host ``tmp/out`` still receives artifacts when
``runtime.mode == container``. This adapter never sets Docker/Podman
``-e`` flags or mounts volumes itself.

Future composition (cache-model.md, AD-9):
``populate_via_staging(cache_entry_path(state_root, "palettes", ph),
lambda staging: CsgAdapter().generate(wallpaper_path, staging))``
satisfies container forwarding automatically because ``generate``'s env
injection works identically per-call. The adapter itself never calls
``populate_via_staging`` — caller owns staging lifecycle and cleanup.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from runtime.adapters.env import build_env
from runtime.adapters.hashing import (
    HASH_ALGORITHM,
    canonical_hash_dir,
    hash_file,
    palette_entry_hash_for_producer,
)
from runtime.domain.models import (
    PALETTE_ARTIFACT_NAMES,
    PaletteArtifacts,
    PaletteEntry,
    PaletteUnavailableError,
)
from runtime.ports.color_scheme_generator import IColorSchemeGenerator

logger = logging.getLogger(__name__)

# Guard from Story 1.6 learnings — fail-closed on algorithm drift (not assert)
if HASH_ALGORITHM != "sha256":  # pragma: no cover
    raise AssertionError(f"HASH_ALGORITHM must be sha256, got {HASH_ALGORITHM}")

_EMPTY_DIR_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

#: csg's own container-exec timeout (``[container].timeout_seconds``; csg 0.2.0
#: defaults to 300 s). The adapter must outlive it so csg's ``finally`` block
#: removes the container before the host process is killed (BUG-002).
CSG_INTERNAL_TIMEOUT_SECONDS: Final[int] = 300

#: Extra time above csg's internal timeout for that cleanup window.
CSG_TIMEOUT_GRACE_SECONDS: Final[int] = 30

#: Production default: 330 s, deliberately above csg's 300 s.
DEFAULT_TIMEOUT_SECONDS: Final[int] = CSG_INTERNAL_TIMEOUT_SECONDS + CSG_TIMEOUT_GRACE_SECONDS

#: Grace after SIGINT (which lets csg's Python ``finally`` remove the
#: container) before escalating to SIGKILL. Measured real-container cleanup
#: after SIGINT was ~12.2 s for the 8K fixture, so 30 s leaves headroom.
_TERMINATION_GRACE_SECONDS: Final[float] = 30.0

#: Pipe-drain bound after SIGKILL; a process that cannot be reaped is reported.
_REAP_TIMEOUT_SECONDS: Final[float] = 5.0


@dataclass(frozen=True, slots=True)
class _CsgRunResult:
    """Outcome of one ``csg generate`` subprocess, including timeout handling."""

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
    """Reap a timed-out child: SIGINT first (csg cleanup), then SIGKILL.

    SIGINT reaches csg's main thread as ``KeyboardInterrupt``; csg's
    ``OciContainerRuntimeAdapter.run`` has a ``finally`` that force-removes the
    container. The grace period lets that cleanup finish. A child that ignores
    SIGINT is killed, so the adapter never hangs unbounded.
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


def _run_csg(args: list[str], env: dict[str, str], timeout: int) -> _CsgRunResult:
    """Run ``csg generate`` with a graceful timeout cleanup window.

    ``subprocess.run(timeout=...)`` SIGKILLs the child, so csg never runs its
    container-removal ``finally``. Using ``Popen`` + SIGINT gives csg that
    chance; SIGKILL remains the bounded fallback.
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
        return _CsgRunResult(
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
            elapsed_seconds=time.monotonic() - started,
            termination=termination,
        )
    return _CsgRunResult(
        returncode=proc.returncode,
        stdout=_as_text(stdout),
        stderr=_as_text(stderr),
        timed_out=False,
        elapsed_seconds=time.monotonic() - started,
    )


def _inspect_artifacts(output_dir: Path) -> tuple[list[str], list[str]]:
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


def _build_palette_entry(
    output_dir: Path,
    *,
    entry_hash: str,
    wallpaper_hash: str,
    template_set_hash: str,
    producer: str = "csg",
) -> PaletteEntry:
    """Hash the verified 8-artifact set and build the ``PaletteEntry``."""
    # Compute artifact hashes via binary chunked hash_file — wrap TOCTOU
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


def _validate_csg_bin(csg_bin: str | None) -> str | None:
    if csg_bin is None:
        return None
    if csg_bin == "":
        raise ValueError("csg_bin cannot be empty string (use None for default 'csg')")
    # Reject shell metachars even though shell=False — defense in depth
    if any(c in csg_bin for c in ";|&`$"):
        raise ValueError(f"csg_bin contains shell metacharacter: {csg_bin!r}")
    return csg_bin


def _validate_output_dir(output_dir: Path, expected_hash: str) -> None:
    # Reject empty/degenerate paths
    if str(output_dir) in ("", "."):
        raise ValueError(f"output_dir must be a non-empty path, got {output_dir!r}")
    # Reject traversal / current-dir components
    if ".." in output_dir.parts:
        raise ValueError(f"output_dir must not contain '..': {output_dir}")
    # Reject symlinked output_dir (leaks outside cache/palettes)
    try:
        if output_dir.is_symlink():
            raise ValueError(f"output_dir must not be a symlink: {output_dir}")
        # Also reject if parent is symlink that would redirect
        if output_dir.parent.is_symlink():
            raise ValueError(f"output_dir parent must not be a symlink: {output_dir.parent}")
    except OSError:
        # is_symlink may raise on permission; treat as invalid
        pass
    # Case-sensitive exact match (ph is lowercase hex)
    if output_dir.name != expected_hash:
        raise ValueError(
            f"output_dir hash mismatch: expected {expected_hash}, got {output_dir.name}"
        )
    # Length guard — avoid ENAMETOOLONG / E2BIG
    if len(str(output_dir)) > 4000:
        raise ValueError(f"output_dir path too long ({len(str(output_dir))} chars): {output_dir}")


def _validate_wallpaper(wallpaper_path: Path) -> str:
    """Validate a wallpaper input and return its SHA-256 hex hash."""
    if str(wallpaper_path) in ("", "."):
        raise ValueError(f"wallpaper_path must be non-empty, got {wallpaper_path!r}")
    if not wallpaper_path.exists():
        raise FileNotFoundError(wallpaper_path)
    if wallpaper_path.is_dir():
        raise IsADirectoryError(wallpaper_path)
    if not wallpaper_path.is_file():
        # FIFO, socket, device, broken symlink, etc. — not a regular file
        raise ValueError(f"wallpaper_path is not a regular file: {wallpaper_path}")
    # Size / type guard — avoid hashing /dev/zero, huge files, empty
    try:
        st = wallpaper_path.stat()
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"wallpaper_path is not a regular file: {wallpaper_path}")
        if st.st_size == 0:
            raise ValueError(f"wallpaper_path is empty (0 bytes): {wallpaper_path}")
        # 100 MB cap — prevents DoS before subprocess timeout
        if st.st_size > 100 * 1024 * 1024:
            raise ValueError(f"wallpaper_path too large ({st.st_size} bytes): {wallpaper_path}")
    except OSError as exc:
        # PermissionError / OSError from stat — surface as runtime error
        if isinstance(exc, (FileNotFoundError, IsADirectoryError, ValueError)):
            raise
        raise RuntimeError(f"cannot stat wallpaper_path {wallpaper_path}: {exc}") from exc

    # Compute wallpaper hash via chunked binary read — wrap permission errors
    try:
        return hash_file(wallpaper_path)
    except (PermissionError, OSError) as exc:
        raise RuntimeError(f"cannot hash wallpaper_path {wallpaper_path}: {exc}") from exc


class CsgAdapter(IColorSchemeGenerator):
    """Adapter that invokes ``csg generate`` via env override.

    Location: ``src/runtime/adapters/csg_adapter.py``
    (chosen over ``csg.py`` to avoid shadowing external
    ``color_scheme_generator`` package).

    Timeout coupling (BUG-002): csg runs the container with its own
    ``[container].timeout_seconds`` (300 s) and removes it in a Python
    ``finally``. The adapter default is therefore
    :data:`DEFAULT_TIMEOUT_SECONDS` (330 s) so csg's cleanup wins the race.
    An explicit lower timeout is allowed for tests/forced-timeout proofs, but
    it cannot let csg's internal cleanup run; the adapter then SIGINTs the
    child so csg can still execute ``finally``, and verifies the artifacts
    either way instead of discarding a complete palette.
    """

    DEFAULT_TIMEOUT: int = DEFAULT_TIMEOUT_SECONDS

    producer_name: str = "csg"

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        csg_bin: str | None = None,
        templates_dir: Path | None = None,
    ) -> None:
        if not isinstance(timeout, int):
            raise ValueError(f"timeout must be int, got {type(timeout).__name__}")
        if timeout <= 0:
            raise ValueError(f"timeout must be > 0, got {timeout}")
        self._timeout = timeout
        self._csg_bin = _validate_csg_bin(csg_bin)
        self._templates_dir = templates_dir

    @property
    def templates_dir(self) -> Path | None:
        """The spine-resolved CSG templates dir (``None`` when not supplied)."""
        return self._templates_dir

    def _resolve_template_set_hash(self) -> str:
        """Validate the templates dir and return its canonical hash.

        ``templates_dir`` is REQUIRED and must be resolved from the
        provisioning spine by the caller (``find_templates_dir(install_spine)``).
        There is no repo-relative discovery fallback: the in-tree csg checkout
        was retired with cli-tools-repos S5, so a discovered path would be dead
        on a provisioned host and divergent on a dev checkout. Failing here
        (not in ``__init__``) keeps bare construction for ``is_available()``
        legitimate.
        """
        templates_dir = self._templates_dir
        if templates_dir is None:
            raise FileNotFoundError(
                "CSG templates dir not provided: CsgAdapter was constructed without "
                "templates_dir. Resolve it from the provisioning spine "
                "(runtime.application.derive.find_templates_dir(install_spine)) and pass "
                "templates_dir=... to the caller's CsgAdapter construction; the retired "
                "repo-relative discovery fallback has been removed."
            )
        if not templates_dir.exists():
            raise FileNotFoundError(f"CSG templates dir not found: {templates_dir}")
        if not templates_dir.is_dir():
            raise NotADirectoryError(f"CSG templates dir is not a directory: {templates_dir}")
        # Empty dir check — canonical_hash_dir returns sha256(b"") for empty
        try:
            if not any(templates_dir.iterdir()):
                raise FileNotFoundError(f"CSG templates dir is empty: {templates_dir}")
        except OSError as exc:
            raise RuntimeError(f"cannot read templates dir {templates_dir}: {exc}") from exc
        try:
            template_set_hash = canonical_hash_dir(templates_dir)
        except OSError as exc:
            raise RuntimeError(f"cannot hash templates dir {templates_dir}: {exc}") from exc
        if template_set_hash == _EMPTY_DIR_HASH:
            raise FileNotFoundError(f"CSG templates dir is empty (hash is empty): {templates_dir}")
        return template_set_hash

    def template_set_hash(self) -> str:
        """Canonical hash of the CSG template set (same value ``generate`` uses)."""
        return self._resolve_template_set_hash()

    def expected_entry_hash(self, wallpaper_hash: str) -> str:
        """CSG's palette entry key: legacy formula, seed ``None`` (unchanged)."""
        return palette_entry_hash_for_producer(
            self.producer_name, wallpaper_hash, self.template_set_hash()
        )

    def is_available(self) -> bool:
        """Return True if ``csg`` binary is on PATH and executable."""
        bin_name = self._csg_bin or "csg"
        # Path with separator — check file existence + executable bit
        if (
            os.path.sep in bin_name
            or (os.path.altsep and os.path.altsep in bin_name)
            or "\\" in bin_name
        ):
            p = Path(bin_name)
            if p.is_file():
                # Check executable bit where possible
                try:
                    return os.access(p, os.X_OK)
                except OSError:
                    return False
            return shutil.which(bin_name) is not None
        result = shutil.which(bin_name)
        if result is None:
            return False
        # which found something — verify executable
        try:
            return os.access(result, os.X_OK)
        except OSError:
            return True  # which succeeded, be permissive

    def generate(self, wallpaper_path: Path, output_dir: Path) -> PaletteEntry:
        """Generate palette from wallpaper via ``csg`` with env override.

        Args:
            wallpaper_path: absolute or relative Path to image file (must exist, is_file).
            output_dir: target palette entry dir (state_root/cache/palettes/<ph>),
                must have name == palette_entry_hash(wallpaper_hash, template_set_hash).
                Parent is created if missing; the dir itself is created if needed.

        Returns:
            PaletteEntry with artifact_hashes + generated_at.

        Raises:
            FileNotFoundError: if wallpaper_path missing, templates_dir was not
                supplied (it must be resolved from the provisioning spine by the
                caller), or csg not on PATH.
            IsADirectoryError: if wallpaper_path is a directory.
            ValueError: if output_dir name does not match computed entry hash.
            RuntimeError: on non-zero csg exit with stderr truncated to 2 KiB,
                or when a successful (non-timeout) run lacks an artifact.
            PaletteUnavailableError: when csg times out and the 8-artifact set
                is incomplete (message carries elapsed time, termination
                signal, missing/non-file artifact names, rc and stderr tail).
                A timeout with a complete set returns a normal ``PaletteEntry``.
        """
        # 1. Validate wallpaper_path and hash it before spawning a subprocess
        wallpaper_hash = _validate_wallpaper(wallpaper_path)

        # 2. Resolve templates dir READ-ONLY and compute template_set_hash.
        #    See ``_resolve_template_set_hash`` for the spine-only contract.
        template_set_hash = self._resolve_template_set_hash()

        # 3. Compute entry hash and validate output_dir name (strict, case-sensitive)
        ph = self.expected_entry_hash(wallpaper_hash)
        _validate_output_dir(output_dir, ph)

        # 5. Ensure parent exists; create output_dir if needed (staging case already exists)
        # Wrap mkdir collisions (FileExistsError when path is file) explicitly
        try:
            output_dir.parent.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise RuntimeError(f"output_dir parent exists as file: {output_dir.parent}") from exc
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.EISDIR):
                raise RuntimeError(
                    f"output_dir parent collision: {output_dir.parent}: {exc}"
                ) from exc
            raise RuntimeError(
                f"cannot create output_dir parent {output_dir.parent}: {exc}"
            ) from exc
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise RuntimeError(f"output_dir exists as file, not dir: {output_dir}") from exc
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.EISDIR):
                raise RuntimeError(f"output_dir collision: {output_dir}: {exc}") from exc
            raise RuntimeError(f"cannot create output_dir {output_dir}: {exc}") from exc

        # 6. Build env with allowlisted host vars + literal double-underscore keys (AD-7)
        # Uses centralized env builder to avoid secret exfiltration into container
        env = build_env(
            {
                "COLORSCHEME__OUTPUT__DIRECTORY": str(output_dir),
                "COLORSCHEME__OUTPUT__OVERWRITE": "true",
            }
        )

        # 7. Build args — intentionally NO -o/--output flag, only --format flags
        bin_name = self._csg_bin or "csg"
        args = [
            bin_name,
            "generate",
            str(wallpaper_path),
            "--format",
            "yaml",
            "--format",
            "conf",
            "--format",
            "gtk.css",
            "--format",
            "adw.css",
            "--format",
            "sequences",
            "--format",
            "rasi",
            "--format",
            "kitty",
            "--format",
            "btop.theme",
        ]

        # 8. Run subprocess with timeout, preserve stderr, handle missing binary.
        #    The timeout path does NOT discard artifacts: a complete palette
        #    already written by csg is accepted (BUG-002).
        try:
            result = _run_csg(args, env, self._timeout)
        except FileNotFoundError as exc:
            # Binary not found — normalize message for caller
            raise FileNotFoundError("csg not on PATH") from exc
        except PermissionError as exc:
            # Non-executable binary
            raise FileNotFoundError("csg not on PATH (permission denied)") from exc
        except OSError as exc:
            if exc.errno == errno.E2BIG:
                raise RuntimeError(f"csg generate env too large (E2BIG): {exc}") from exc
            if exc.errno == errno.ENOENT:
                raise FileNotFoundError("csg not on PATH") from exc
            raise RuntimeError(f"csg generate failed to spawn: {exc}") from exc

        # 8a. Timeout: verify the complete 8-artifact set first. The child was
        #     signalled so csg's own container-removal finally could run; if a
        #     complete palette is on disk it is usable and MUST NOT be thrown
        #     away by the caller's staging cleanup.
        if result.timed_out:
            missing, not_files = _inspect_artifacts(output_dir)
            if not missing and not not_files:
                logger.warning(
                    "csg generate exceeded %ss (elapsed %.1fs, terminated_by=%s) "
                    "but produced the complete 8-artifact palette; accepting it",
                    self._timeout,
                    result.elapsed_seconds,
                    result.termination,
                )
                return _build_palette_entry(
                    output_dir,
                    entry_hash=ph,
                    wallpaper_hash=wallpaper_hash,
                    template_set_hash=template_set_hash,
                )

            detail_parts: list[str] = []
            if missing:
                detail_parts.append("missing=" + ",".join(missing))
            if not_files:
                detail_parts.append("not_files=" + ",".join(not_files))
            detail = "; ".join(detail_parts)
            stderr_part = result.stderr[:2048]
            msg = (
                f"csg generate timed out after {self._timeout}s "
                f"(elapsed {result.elapsed_seconds:.1f}s, "
                f"terminated_by={result.termination}); incomplete csg output: {detail}"
            )
            if stderr_part:
                msg += f": {stderr_part}"
            raise PaletteUnavailableError(
                msg,
                wallpaper_path=str(wallpaper_path),
                wallpaper_hash=wallpaper_hash,
                returncode=result.returncode,
                stderr=stderr_part,
                hint=(
                    "csg timed out and the palette artifact set is incomplete; "
                    "check csg/container logs, then re-set the wallpaper"
                ),
            )

        if result.returncode != 0:
            # Negative returncode means killed by signal
            if result.returncode is not None and result.returncode < 0:
                sig = -result.returncode
                try:
                    sig_name = signal.Signals(sig).name
                except Exception:
                    sig_name = str(sig)
                stderr = result.stderr[:2048]
                stdout_part = result.stdout[:500]
                raise PaletteUnavailableError(
                    f"csg killed by signal {sig_name} ({sig}): {stderr} stdout:{stdout_part}",
                    wallpaper_path=str(wallpaper_path),
                    wallpaper_hash=wallpaper_hash,
                    returncode=result.returncode,
                    stderr=stderr,
                )
            stderr = result.stderr[:2048]
            stdout = result.stdout[:500]
            raise PaletteUnavailableError(
                f"csg generate failed (exit {result.returncode}): {stderr} stdout:{stdout}",
                wallpaper_path=str(wallpaper_path),
                wallpaper_hash=wallpaper_hash,
                returncode=result.returncode,
                stderr=stderr,
            )

        # 9. Verify artifacts exist as files (not dirs)
        missing, not_files = _inspect_artifacts(output_dir)
        if missing or not_files:
            name = (missing or not_files)[0]
            p = output_dir / name
            if name in not_files:
                raise RuntimeError(f"csg output {name} is a directory, expected file: {p}")
            raise RuntimeError(f"csg did not write expected artifact: {p}")

        # 10. Compute artifact hashes and return the entry
        return _build_palette_entry(
            output_dir,
            entry_hash=ph,
            wallpaper_hash=wallpaper_hash,
            template_set_hash=template_set_hash,
        )
