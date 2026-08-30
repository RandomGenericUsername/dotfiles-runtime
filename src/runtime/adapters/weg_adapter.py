"""WEG adapter with env override (Story 1.8, AD-7).

Directs ``weg batch all`` output via ``WALLPAPER__OUTPUT__DIRECTORY``
env override into ``cache/effects/<eh>/`` without editing
provisioning's ``settings.toml``.

Container forwarding (AD-7 last sentence):
``weg`` runs in container mode (podman/docker via ``oci-runtime``):
the output-dir override must be passed **INTO** the container environment,
not just the host process. This adapter only sets the host env dict and
passes it to ``subprocess.run(env=)`` — ``weg``'s own
``container_processor`` forwards the env into ``RunConfig`` via
``oci_runtime.ContainerRuntimePort``. Verified by CSG determinism zero
``-o`` flag pattern where host ``tmp/out`` still receives artifacts when
``runtime.mode == container``. This adapter never sets Docker/Podman
``-e`` flags or mounts volumes itself.

Future composition (cache-model.md, AD-9):
``populate_via_staging(cache_entry_path(state_root, "effects", eh),
lambda staging: WegAdapter().generate(wallpaper_path, staging))``
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
    effects_entry_hash,
    hash_file,
)
from runtime.domain.models import EffectsEntry
from runtime.ports.effects_generator import IEffectsGenerator

# Guard from Story 1.6 learnings — fail-closed on algorithm drift (not assert)
if HASH_ALGORITHM != "sha256":  # pragma: no cover
    raise AssertionError(f"HASH_ALGORITHM must be sha256, got {HASH_ALGORITHM}")


def _find_default_effects_catalog() -> Path | None:
    """Search repo for ``wallpaper_effects_generator/defaults/effects.yaml``."""
    for parent in Path(__file__).resolve().parents:
        candidates = [
            parent
            / "src"
            / "cli-tools"
            / "wallpaper-effects-generator"
            / "src"
            / "wallpaper_effects_generator"
            / "defaults"
            / "effects.yaml",
            parent
            / "src"
            / "cli-tools"
            / "wallpaper-effects-generator"
            / "defaults"
            / "effects.yaml",
        ]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except PermissionError:
                continue
    return None


def _validate_weg_bin(weg_bin: str | None) -> str | None:
    if weg_bin is None:
        return None
    if weg_bin == "":
        raise ValueError("weg_bin cannot be empty string (use None for default 'weg')")
    if any(c in weg_bin for c in ";|&`$"):
        raise ValueError(f"weg_bin contains shell metacharacter: {weg_bin!r}")
    return weg_bin


def _validate_output_dir(output_dir: Path, expected_hash: str) -> None:
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
        pass
    if output_dir.name != expected_hash:
        raise ValueError(
            f"output_dir hash mismatch: expected {expected_hash}, got {output_dir.name}"
        )
    if len(str(output_dir)) > 4000:
        raise ValueError(f"output_dir path too long ({len(str(output_dir))} chars): {output_dir}")


class WegAdapter(IEffectsGenerator):
    """Adapter that invokes ``weg batch all`` via env override.

    Location: ``src/runtime/src/runtime/adapters/weg_adapter.py``
    (mirrors ``csg_adapter.py`` for layered cache).
    """

    def __init__(
        self,
        timeout: int = 60,
        weg_bin: str | None = None,
        catalog_path: Path | None = None,
    ) -> None:
        if not isinstance(timeout, int):
            raise ValueError(f"timeout must be int, got {type(timeout).__name__}")
        if timeout <= 0:
            raise ValueError(f"timeout must be > 0, got {timeout}")
        self._timeout = timeout
        self._weg_bin = _validate_weg_bin(weg_bin)
        self._catalog_path = catalog_path

    def is_available(self) -> bool:
        """Return True if ``weg`` binary is on PATH and executable."""
        bin_name = self._weg_bin or "weg"
        if (
            os.path.sep in bin_name
            or (os.path.altsep and os.path.altsep in bin_name)
            or "\\" in bin_name
        ):
            p = Path(bin_name)
            if p.is_file():
                try:
                    return os.access(p, os.X_OK)
                except OSError:
                    return False
            return shutil.which(bin_name) is not None
        result = shutil.which(bin_name)
        if result is None:
            return False
        try:
            return os.access(result, os.X_OK)
        except OSError:
            return True

    def generate(self, wallpaper_path: Path, output_dir: Path) -> EffectsEntry:
        """Generate effects from wallpaper via ``weg`` with env override.

        Args:
            wallpaper_path: absolute or relative Path to image file (must exist, is_file).
            output_dir: target effects entry dir (state_root/cache/effects/<eh>),
                must have name == effects_entry_hash(wallpaper_hash, catalog_hash).
                Parent is created if missing; the dir itself is created if needed.

        Returns:
            EffectsEntry with artifact_hashes + generated_at.

        Raises:
            FileNotFoundError: if wallpaper_path missing or catalog not found
                or weg not on PATH.
            IsADirectoryError: if wallpaper_path is a directory.
            ValueError: if output_dir name does not match computed entry hash.
            RuntimeError: on non-zero weg exit with stderr truncated to 2 KiB.
            TimeoutError: if weg hangs beyond timeout.
        """
        # 1. Validate wallpaper_path before spawning subprocess
        if str(wallpaper_path) in ("", "."):
            raise ValueError(f"wallpaper_path must be non-empty, got {wallpaper_path!r}")
        if not wallpaper_path.exists():
            raise FileNotFoundError(wallpaper_path)
        if wallpaper_path.is_dir():
            raise IsADirectoryError(wallpaper_path)
        if not wallpaper_path.is_file():
            raise ValueError(f"wallpaper_path is not a regular file: {wallpaper_path}")
        try:
            st = wallpaper_path.stat()
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"wallpaper_path is not a regular file: {wallpaper_path}")
            if st.st_size == 0:
                raise ValueError(f"wallpaper_path is empty (0 bytes): {wallpaper_path}")
            if st.st_size > 100 * 1024 * 1024:
                raise ValueError(f"wallpaper_path too large ({st.st_size} bytes): {wallpaper_path}")
        except OSError as exc:
            if isinstance(exc, (FileNotFoundError, IsADirectoryError, ValueError)):
                raise
            raise RuntimeError(f"cannot stat wallpaper_path {wallpaper_path}: {exc}") from exc

        # 2. Compute wallpaper hash via chunked binary read
        try:
            wallpaper_hash = hash_file(wallpaper_path)
        except (PermissionError, OSError) as exc:
            raise RuntimeError(f"cannot hash wallpaper_path {wallpaper_path}: {exc}") from exc

        # 3. Resolve catalog path READ-ONLY and compute catalog_hash
        catalog_path = self._catalog_path
        if catalog_path is None:
            catalog_path = _find_default_effects_catalog()
        if catalog_path is None:
            raise FileNotFoundError("WEG catalog not found: no default effects.yaml discovered")
        if not catalog_path.exists():
            raise FileNotFoundError(f"WEG catalog not found: {catalog_path}")
        if not catalog_path.is_file():
            raise IsADirectoryError(f"WEG catalog is not a file: {catalog_path}")
        try:
            c_st = catalog_path.stat()
            if c_st.st_size == 0:
                raise ValueError(f"WEG catalog is empty (0 bytes): {catalog_path}")
        except OSError as exc:
            if isinstance(exc, (FileNotFoundError, IsADirectoryError, ValueError)):
                raise
            raise RuntimeError(f"cannot stat catalog {catalog_path}: {exc}") from exc
        try:
            catalog_hash = hash_file(catalog_path)
        except (PermissionError, OSError) as exc:
            raise RuntimeError(f"cannot hash catalog {catalog_path}: {exc}") from exc

        # 4. Compute entry hash and validate output_dir name (strict, case-sensitive)
        eh = effects_entry_hash(wallpaper_hash, catalog_hash)
        _validate_output_dir(output_dir, eh)

        # 5. Ensure parent exists; create output_dir if needed
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

        # 6. Build env with allowlisted host vars + literal double-underscore key (AD-7)
        env = build_env(
            {
                "WALLPAPER__OUTPUT__DIRECTORY": str(output_dir),
            }
        )

        # 7. Build args — intentionally NO -o/--output flag, only positional wallpaper_path
        bin_name = self._weg_bin or "weg"
        args = [
            bin_name,
            "batch",
            "all",
            str(wallpaper_path),
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
            raise FileNotFoundError("weg not on PATH") from exc
        except PermissionError as exc:
            raise FileNotFoundError("weg not on PATH (permission denied)") from exc
        except subprocess.TimeoutExpired as exc:
            stderr_part = str(exc.stderr or "")[:2048]
            msg = f"weg batch all timed out after {self._timeout}s"
            if stderr_part:
                msg += f": {stderr_part}"
            raise TimeoutError(msg) from exc
        except OSError as exc:
            if exc.errno == errno.E2BIG:
                raise RuntimeError(f"weg batch all env too large (E2BIG): {exc}") from exc
            if exc.errno == errno.ENOENT:
                raise FileNotFoundError("weg not on PATH") from exc
            raise RuntimeError(f"weg batch all failed to spawn: {exc}") from exc

        if result.returncode != 0:
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
                    f"weg killed by signal {sig_name} ({sig}): {stderr} stdout:{stdout_part}"
                )
            stderr = (result.stderr or "")[:2048]
            stdout = (result.stdout or "")[:500]
            raise RuntimeError(
                f"weg batch all failed (exit {result.returncode}): {stderr} stdout:{stdout}"
            )

        # 9. Verify PNG artifacts exist (recursive, weg nests as output_dir/<stem>/effect/*.png)
        png_files = [p for p in output_dir.rglob("*.png") if p.is_file()]
        if not png_files:
            # Check if output_dir has any files at all for better error
            any_files = [p for p in output_dir.rglob("*") if p.is_file()]
            raise RuntimeError(
                f"weg did not write expected PNG artifacts: {output_dir} "
                f"(found {len(any_files)} files, 0 png)"
            )

        # 10. Compute artifact hashes via binary chunked hash_file — wrap TOCTOU
        # Use basename as key when unique, else relative posix to preserve uniqueness
        # First check for duplicate basenames
        basename_counts: dict[str, int] = {}
        for p in png_files:
            basename_counts[p.name] = basename_counts.get(p.name, 0) + 1

        artifact_hashes: dict[str, str] = {}
        try:
            for p in png_files:
                h = hash_file(p)
                # Key: basename if unique, else relative path
                if basename_counts[p.name] == 1:
                    key = p.name
                else:
                    key = p.relative_to(output_dir).as_posix()
                # Double-check uniqueness after fallback (should be unique now)
                if key in artifact_hashes:
                    # Extremely unlikely duplicate relative — suffix with hash prefix
                    key = f"{key}:{h[:8]}"
                artifact_hashes[key] = h
        except (FileNotFoundError, PermissionError, IsADirectoryError, OSError) as exc:
            raise RuntimeError(f"cannot hash weg artifacts in {output_dir}: {exc}") from exc

        # Verify all hashes are 64-char hex
        for k, v in artifact_hashes.items():
            if len(v) != 64 or not all(c in "0123456789abcdef" for c in v.lower()):
                raise RuntimeError(f"artifact hash for {k} is not 64-char hex: {v!r}")

        generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        return EffectsEntry(
            hash_algorithm=HASH_ALGORITHM,
            kind="effects",
            entry_hash=eh,
            source_wallpaper_hash=wallpaper_hash,
            input_catalog_hash=catalog_hash,
            artifact_hashes=artifact_hashes,  # type: ignore[arg-type]
            generated_at=generated_at,
        )
