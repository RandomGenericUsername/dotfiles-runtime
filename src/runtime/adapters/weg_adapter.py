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
from typing import cast

from runtime.adapters.env import build_env
from runtime.adapters.hashing import (
    HASH_ALGORITHM,
    effects_entry_hash,
    hash_file,
)
from runtime.domain.models import EffectsArtifacts, EffectsEntry
from runtime.ports.effects_generator import IEffectsGenerator

# Guard from Story 1.6 learnings — fail-closed on algorithm drift (not assert)
if HASH_ALGORITHM != "sha256":  # pragma: no cover
    raise AssertionError(f"HASH_ALGORITHM must be sha256, got {HASH_ALGORITHM}")


def _find_default_effects_catalog() -> Path | None:
    """Search for ``effects.yaml`` — mirrors WEG's config-assembler XDG strategy.

    WEG itself discovers its effects catalog via the config-assembler-engine's
    ``CompositePathResolver`` with four strategies in this priority order
    (see ``yaml_effect_loader.py`` in wallpaper-effects-generator):

    1. ``CliPathStrategy`` — ``--config`` flag (handled by the CLI, not here)
    2. ``EnvPathStrategy`` — ``WALLPAPER_EFFECTS_CONFIG_FILE_PATH`` env var
    3. ``DirectoryTraversalStrategy`` — ``effects.yaml`` in CWD or up to
       2 parent levels (``EFFECTS_TRAVERSAL_DEPTH=2``)
    4. ``XdgStrategy`` — ``$XDG_CONFIG_HOME/weg/effects.yaml``
       (``EFFECTS_XDG_SUBDIR="weg"``, ``EFFECTS_FILENAME="effects.yaml"``)

    The provisioning (``config_links`` role, Story 3.4) creates the symlink
    ``~/.config/weg → <install>/config/weg``, so WEG's XDG strategy finds
    the project's catalog through the link. This function mirrors that
    same XDG path so the runtime's pre-computed ``eeh`` matches WEG's.

    A repo-ancestor fallback is included for dev checkouts where neither
    the symlink nor a nearby CWD parent has the catalog.
    """
    # 1) env override (WEG's EnvPathStrategy)
    env_path = os.environ.get("WALLPAPER_EFFECTS_CONFIG_FILE_PATH")
    if env_path:
        candidate = Path(env_path)
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            pass

    # 2) XDG default (WEG's XdgStrategy with EFFECTS_XDG_SUBDIR="weg",
    # EFFECTS_FILENAME="effects.yaml") — symlink-resolved on provisioned hosts
    xdg_config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    xdg_candidate = Path(xdg_config) / "weg" / "effects.yaml"
    try:
        if xdg_candidate.is_file():
            return xdg_candidate
    except OSError:
        pass

    # 3) directory traversal (WEG's DirectoryTraversalStrategy, max 2 levels)
    try:
        cwd = Path.cwd()
    except OSError:
        cwd = None
    if cwd is not None:
        for ancestor in [cwd, *cwd.parents][:3]:  # cwd + 2 parents
            candidate = ancestor / "effects.yaml"
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue

    # 4) repo ancestor fallback (dev checkouts)
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
            except OSError:
                continue
    return None


def _validate_weg_bin(weg_bin: str | None) -> str | None:
    if weg_bin is None:
        return None
    if weg_bin == "":
        raise ValueError("weg_bin cannot be empty string (use None for default 'weg')")
    # Reject control chars, whitespace, null byte, and shell metachars.
    # With shell=False injection still matters via arg-injection; block broadly.
    if "\x00" in weg_bin or "\n" in weg_bin or "\r" in weg_bin:
        raise ValueError(f"weg_bin contains control character: {weg_bin!r}")
    if any(c in weg_bin for c in " \t;|&`$><*?~!()[]{}#\\'\""):
        raise ValueError(f"weg_bin contains shell/whitespace metacharacter: {weg_bin!r}")
    return weg_bin


def _validate_output_dir(output_dir: Path, expected_hash: str) -> None:
    s = str(output_dir)
    if "\x00" in s:
        raise ValueError(f"output_dir contains null byte: {output_dir!r}")
    if s in ("", "."):
        raise ValueError(f"output_dir must be a non-empty path, got {output_dir!r}")
    if ".." in output_dir.parts or "." in output_dir.parts:
        raise ValueError(f"output_dir must not contain '.' or '..': {output_dir}")
    # Symlink check — fail-closed: propagate OSError instead of swallowing
    if output_dir.is_symlink():
        raise ValueError(f"output_dir must not be a symlink: {output_dir}")
    if output_dir.parent.is_symlink():
        raise ValueError(f"output_dir parent must not be a symlink: {output_dir.parent}")
    # Also reject wallpaper-style symlink escape via ancestors: check if any
    # parent component up to two levels is a symlink (covers grandparent)
    try:
        for p in (output_dir.parent, output_dir.parent.parent):
            if p != Path(".") and p.exists() and p.is_symlink():
                raise ValueError(f"output_dir ancestor must not be a symlink: {p}")
    except OSError as exc:
        raise RuntimeError(f"cannot validate output_dir symlink: {exc}") from exc
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
        if type(timeout) is not int:
            raise ValueError(f"timeout must be int, got {type(timeout).__name__}")
        if timeout <= 0:
            raise ValueError(f"timeout must be > 0, got {timeout}")
        if catalog_path is not None and not isinstance(catalog_path, Path):
            raise TypeError(f"catalog_path must be Path or None, got {type(catalog_path).__name__}")
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
            return False

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
        wp_str = str(wallpaper_path)
        if "\x00" in wp_str:
            raise ValueError(f"wallpaper_path contains null byte: {wallpaper_path!r}")
        if wp_str in ("", "."):
            raise ValueError(f"wallpaper_path must be non-empty, got {wallpaper_path!r}")
        # Reject symlink wallpaper (arbitrary file read)
        try:
            if wallpaper_path.is_symlink():
                raise ValueError(f"wallpaper_path must not be a symlink: {wallpaper_path}")
        except OSError as exc:
            raise RuntimeError(f"cannot validate wallpaper_path symlink: {exc}") from exc
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
        if "\x00" in str(catalog_path):
            raise ValueError(f"catalog_path contains null byte: {catalog_path!r}")
        try:
            if catalog_path.is_symlink():
                raise ValueError(f"catalog_path must not be a symlink: {catalog_path}")
        except OSError as exc:
            raise RuntimeError(f"cannot validate catalog symlink: {exc}") from exc
        if not catalog_path.exists():
            raise FileNotFoundError(f"WEG catalog not found: {catalog_path}")
        if not catalog_path.is_file():
            raise IsADirectoryError(f"WEG catalog is not a file: {catalog_path}")
        try:
            c_st = catalog_path.stat()
            if not stat.S_ISREG(c_st.st_mode):
                raise ValueError(f"catalog_path is not a regular file: {catalog_path}")
            if c_st.st_size == 0:
                raise FileNotFoundError(f"WEG catalog is empty (0 bytes): {catalog_path}")
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
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.EISDIR, errno.ENOTDIR):
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
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.EISDIR, errno.ENOTDIR):
                raise RuntimeError(f"output_dir collision: {output_dir}: {exc}") from exc
            raise RuntimeError(f"cannot create output_dir {output_dir}: {exc}") from exc

        # 6. Build env with allowlisted host vars + literal double-underscore key (AD-7)
        env = build_env(
            {
                "WALLPAPER__OUTPUT__DIRECTORY": str(output_dir),
            }
        )

        # 7. Build args — intentionally NO -o/--output flag, only positional wallpaper_path
        # Use -- separator to prevent leading-dash wallpaper from being parsed as flag
        bin_name = self._weg_bin or "weg"
        args = [
            bin_name,
            "batch",
            "all",
            "--",
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
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError("weg not on PATH") from exc
        except PermissionError as exc:
            raise FileNotFoundError("weg not on PATH (permission denied)") from exc
        except subprocess.TimeoutExpired as exc:
            # Handle both str and bytes stderr (text=True vs errors)
            raw_err = exc.stderr
            if isinstance(raw_err, bytes):
                try:
                    stderr_part = raw_err.decode(errors="replace")[:2048]
                except Exception:
                    stderr_part = str(raw_err)[:2048]
            else:
                stderr_part = str(raw_err or "")[:2048]
            raw_out = getattr(exc, "stdout", None)
            if isinstance(raw_out, bytes):
                try:
                    stdout_part = raw_out.decode(errors="replace")[:500]
                except Exception:
                    stdout_part = str(raw_out)[:500]
            else:
                stdout_part = str(raw_out or "")[:500]
            msg = f"weg batch all timed out after {self._timeout}s"
            if stderr_part:
                msg += f": {stderr_part}"
            if stdout_part:
                msg += f" stdout:{stdout_part}"
            raise TimeoutError(msg) from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"weg batch all failed to spawn (bad arg/encoding): {exc}") from exc
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

        # 9. Verify image artifacts exist (recursive, weg nests as
        # output_dir/<stem>/effect/<artifact>.<ext>). WEG matches the source
        # wallpaper's image extension — PNG wallpapers produce PNG effects,
        # JPG wallpapers produce JPG effects, etc. — so accept any
        # common image extension rather than hardcoding .png.
        try:
            all_files = [p for p in output_dir.rglob("*") if p.is_file()]
        except OSError as exc:
            raise RuntimeError(f"cannot list weg output_dir {output_dir}: {exc}") from exc
        image_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}
        image_files = [p for p in all_files if p.suffix.lower() in image_exts]
        if not image_files:
            raise RuntimeError(
                f"weg did not write expected image artifacts: {output_dir} "
                f"(found {len(all_files)} files, 0 image files)"
            )

        # 10. Compute artifact hashes via binary chunked hash_file — wrap TOCTOU
        # Use basename as key when unique, else relative posix to preserve uniqueness
        # First check for duplicate basenames
        basename_counts: dict[str, int] = {}
        for p in image_files:
            basename_counts[p.name] = basename_counts.get(p.name, 0) + 1

        artifact_hashes: dict[str, str] = {}
        try:
            for p in image_files:
                h = hash_file(p)
                # Key: basename if unique, else relative path (handle symlink escape)
                if basename_counts[p.name] == 1:
                    key = p.name
                else:
                    try:
                        key = p.relative_to(output_dir).as_posix()
                    except ValueError as exc:
                        raise RuntimeError(
                            f"artifact outside output_dir (symlink escape): {p} not in {output_dir}"
                        ) from exc
                # Double-check uniqueness after fallback (should be unique now)
                if key in artifact_hashes:
                    # Extremely unlikely duplicate relative — suffix with hash prefix
                    key = f"{key}:{h[:8]}"
                artifact_hashes[key] = h
        except (FileNotFoundError, PermissionError, IsADirectoryError, OSError) as exc:
            raise RuntimeError(f"cannot hash weg artifacts in {output_dir}: {exc}") from exc
        except ValueError as exc:
            # from relative_to escape already wrapped, but also catch hash_file TypeError etc
            if "outside output_dir" in str(exc):
                raise
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
            artifact_hashes=cast(EffectsArtifacts, artifact_hashes),
            generated_at=generated_at,
        )
