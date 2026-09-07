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
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from runtime.adapters.env import build_env
from runtime.adapters.hashing import (
    HASH_ALGORITHM,
    canonical_hash_dir,
    hash_file,
    palette_entry_hash,
)
from runtime.domain.models import PaletteArtifacts, PaletteEntry
from runtime.ports.color_scheme_generator import IColorSchemeGenerator

# Guard from Story 1.6 learnings — fail-closed on algorithm drift (not assert)
if HASH_ALGORITHM != "sha256":  # pragma: no cover
    raise AssertionError(f"HASH_ALGORITHM must be sha256, got {HASH_ALGORITHM}")

_EMPTY_DIR_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _find_default_templates_dir() -> Path | None:
    """Search repo for ``color_scheme_generator/defaults/templates``."""
    for parent in Path(__file__).resolve().parents:
        candidates = [
            parent
            / "src"
            / "cli-tools"
            / "color-scheme-generator"
            / "src"
            / "color_scheme_generator"
            / "defaults"
            / "templates",
            parent / "src" / "cli-tools" / "color-scheme-generator" / "defaults" / "templates",
        ]
        for candidate in candidates:
            try:
                if candidate.is_dir():
                    return candidate
            except PermissionError:
                # Permission denied — treat as not found but don't silently skip
                # caller will get FileNotFoundError with permission context
                continue
    return None


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


class CsgAdapter(IColorSchemeGenerator):
    """Adapter that invokes ``csg generate`` via env override.

    Location: ``src/runtime/src/runtime/adapters/csg_adapter.py``
    (chosen over ``csg.py`` to avoid shadowing external
    ``color_scheme_generator`` package).
    """

    def __init__(
        self,
        timeout: int = 60,
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
            FileNotFoundError: if wallpaper_path missing or templates dir not found
                or csg not on PATH.
            IsADirectoryError: if wallpaper_path is a directory.
            ValueError: if output_dir name does not match computed entry hash.
            RuntimeError: on non-zero csg exit with stderr truncated to 2 KiB.
            TimeoutError: if csg hangs beyond timeout.
        """
        # 1. Validate wallpaper_path before spawning subprocess
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

        # 2. Compute wallpaper hash via chunked binary read — wrap permission errors
        try:
            wallpaper_hash = hash_file(wallpaper_path)
        except (PermissionError, OSError) as exc:
            raise RuntimeError(f"cannot hash wallpaper_path {wallpaper_path}: {exc}") from exc

        # 3. Resolve templates dir READ-ONLY and compute template_set_hash
        templates_dir = self._templates_dir
        if templates_dir is None:
            templates_dir = _find_default_templates_dir()
        if templates_dir is None:
            raise FileNotFoundError(
                "CSG templates dir not found: no default templates dir discovered"
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

        # 4. Compute entry hash and validate output_dir name (strict, case-sensitive)
        ph = palette_entry_hash(wallpaper_hash, template_set_hash)
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
        ]

        # 8. Run subprocess with timeout, preserve stderr, handle missing binary
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                env=env,
                timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            # Binary not found — normalize message for caller
            raise FileNotFoundError("csg not on PATH") from exc
        except PermissionError as exc:
            # Non-executable binary
            raise FileNotFoundError("csg not on PATH (permission denied)") from exc
        except subprocess.TimeoutExpired as exc:
            # text=True guarantees stderr is str
            stderr_part = str(exc.stderr or "")[:2048]
            msg = f"csg generate timed out after {self._timeout}s"
            if stderr_part:
                msg += f": {stderr_part}"
            raise TimeoutError(msg) from exc
        except OSError as exc:
            if exc.errno == errno.E2BIG:
                raise RuntimeError(f"csg generate env too large (E2BIG): {exc}") from exc
            if exc.errno == errno.ENOENT:
                raise FileNotFoundError("csg not on PATH") from exc
            raise RuntimeError(f"csg generate failed to spawn: {exc}") from exc

        if result.returncode != 0:
            # Negative returncode means killed by signal
            if result.returncode < 0:
                sig = -result.returncode
                try:
                    import signal as _signal

                    sig_name = _signal.Signals(sig).name
                except Exception:
                    sig_name = str(sig)
                stderr = (result.stderr or "")[:2048]
                stdout_part = (result.stdout or "")[:500]
                raise RuntimeError(
                    f"csg killed by signal {sig_name} ({sig}): {stderr} stdout:{stdout_part}"
                )
            stderr = (result.stderr or "")[:2048]
            stdout = (result.stdout or "")[:500]
            raise RuntimeError(
                f"csg generate failed (exit {result.returncode}): {stderr} stdout:{stdout}"
            )

        # 9. Verify artifacts exist as files (not dirs)
        for name in (
            "colors.yaml",
            "colors.conf",
            "colors.gtk.css",
            "colors.adw.css",
            "colors.sequences",
        ):
            p = output_dir / name
            if not p.is_file():
                if p.exists() and p.is_dir():
                    raise RuntimeError(f"csg output {name} is a directory, expected file: {p}")
                raise RuntimeError(f"csg did not write expected artifact: {p}")

        # 10. Compute artifact hashes via binary chunked hash_file — wrap TOCTOU
        try:
            artifact_for_entry: PaletteArtifacts = PaletteArtifacts(
                colors_yaml=hash_file(output_dir / "colors.yaml"),
                colors_conf=hash_file(output_dir / "colors.conf"),
                colors_gtk_css=hash_file(output_dir / "colors.gtk.css"),
                colors_adw_css=hash_file(output_dir / "colors.adw.css"),
                colors_sequences=hash_file(output_dir / "colors.sequences"),
            )
        except (FileNotFoundError, PermissionError, IsADirectoryError, OSError) as exc:
            raise RuntimeError(f"cannot hash csg artifacts in {output_dir}: {exc}") from exc

        generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        return PaletteEntry(
            hash_algorithm=HASH_ALGORITHM,
            kind="palette",
            entry_hash=ph,
            source_wallpaper_hash=wallpaper_hash,
            input_template_hash=template_set_hash,
            artifact_hashes=artifact_for_entry,
            generated_at=generated_at,
        )
