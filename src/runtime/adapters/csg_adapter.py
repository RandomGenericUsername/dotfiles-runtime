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

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from runtime.adapters.hashing import (
    HASH_ALGORITHM,
    canonical_hash_dir,
    hash_file,
    palette_entry_hash,
)
from runtime.domain.models import PaletteArtifacts, PaletteEntry
from runtime.ports.color_scheme_generator import IColorSchemeGenerator


def _find_default_templates_dir() -> Path | None:
    """Search repo for ``color_scheme_generator/defaults/templates``."""
    for parent in Path(__file__).resolve().parents:
        candidate = (
            parent
            / "src"
            / "cli-tools"
            / "color-scheme-generator"
            / "src"
            / "color_scheme_generator"
            / "defaults"
            / "templates"
        )
        if candidate.is_dir():
            return candidate
        alt = parent / "src" / "cli-tools" / "color-scheme-generator" / "defaults" / "templates"
        if alt.is_dir():
            return alt
    return None


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
        self._timeout = timeout
        self._csg_bin = csg_bin
        self._templates_dir = templates_dir

    def is_available(self) -> bool:
        """Return True if ``csg`` binary is on PATH."""
        bin_name = self._csg_bin or "csg"
        # Absolute or relative path with separator — check file existence directly
        if os.path.sep in bin_name:
            return Path(bin_name).is_file() or shutil.which(bin_name) is not None
        return shutil.which(bin_name) is not None

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
        if not wallpaper_path.exists():
            raise FileNotFoundError(wallpaper_path)
        if not wallpaper_path.is_file():
            raise IsADirectoryError(wallpaper_path)

        # 2. Compute wallpaper hash via chunked binary read
        wallpaper_hash = hash_file(wallpaper_path)

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
        template_set_hash = canonical_hash_dir(templates_dir)

        # 4. Compute entry hash and validate output_dir name
        ph = palette_entry_hash(wallpaper_hash, template_set_hash)
        if output_dir.name.lower() != ph:
            raise ValueError(f"output_dir hash mismatch: expected {ph}, got {output_dir.name}")

        # 5. Ensure parent exists; create output_dir if needed (staging case already exists)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        # 6. Build env with literal double-underscore keys (AD-7), no global mutation
        env = dict(os.environ)
        env["COLORSCHEME__OUTPUT__DIRECTORY"] = str(output_dir)
        env["COLORSCHEME__OUTPUT__OVERWRITE"] = "true"

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
        except subprocess.TimeoutExpired as exc:
            stderr_part = ""
            if exc.stderr:
                # TimeoutExpired stderr may be bytes or str depending on text mode
                if isinstance(exc.stderr, bytes):
                    stderr_part = exc.stderr.decode("utf-8", errors="replace")[:2048]
                else:
                    stderr_part = str(exc.stderr)[:2048]
            msg = f"csg generate timed out after {self._timeout}s"
            if stderr_part:
                msg += f": {stderr_part}"
            raise TimeoutError(msg) from exc
        except OSError as exc:
            # Fallback for other spawn failures
            raise RuntimeError(f"csg generate failed to spawn: {exc}") from exc

        if result.returncode != 0:
            stderr = (result.stderr or "")[:2048]
            stdout = (result.stdout or "")[:500]
            raise RuntimeError(
                f"csg generate failed (exit {result.returncode}): {stderr} stdout:{stdout}"
            )

        # 9. Verify artifacts exist as files (not dirs)
        for name in ("colors.yaml", "colors.conf", "colors.gtk.css"):
            p = output_dir / name
            if not p.is_file():
                if p.exists() and p.is_dir():
                    raise RuntimeError(f"csg output {name} is a directory, expected file: {p}")
                raise RuntimeError(f"csg did not write expected artifact: {p}")

        # 10. Compute artifact hashes via binary chunked hash_file (reuses hashing helpers)
        typed_hashes: dict[str, str] = {
            "colors_yaml": hash_file(output_dir / "colors.yaml"),
            "colors_conf": hash_file(output_dir / "colors.conf"),
            "colors_gtk_css": hash_file(output_dir / "colors.gtk.css"),
        }

        artifact_for_entry = cast(
            PaletteArtifacts,
            {
                "colors_yaml": typed_hashes["colors_yaml"],
                "colors_conf": typed_hashes["colors_conf"],
                "colors_gtk_css": typed_hashes["colors_gtk_css"],
            },
        )

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
