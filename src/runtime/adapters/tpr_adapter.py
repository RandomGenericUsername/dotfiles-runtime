"""TPR palette adapter — invokes ``tpr generate`` behind the CSG default.

``tpr`` writes a canonical ``theme.json`` plus eight consumer projections. The
runtime palette entry records only the eight projections (``PaletteArtifact``
contract), so the adapter invokes ``tpr`` in a private staging directory,
copies exactly those eight files into the palette entry, and leaves
``theme.json`` (and any other producer-side output) behind.

The cache key carries a producer fingerprint: ``tpr`` seeds
:func:`runtime.adapters.hashing.palette_entry_hash` with
``sha256("tpr:" + engine + ":" + version + ":" + template_set_hash)``.
``csg`` keeps the legacy seed ``None``, so its keys are byte-identical.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Final

from runtime.adapters.csg_adapter import (
    DEFAULT_TIMEOUT_SECONDS,
    _artifact_failure,
    _build_palette_entry,
    _inspect_artifacts,
    _run_csg,
    _validate_output_dir,
    _validate_wallpaper,
)
from runtime.adapters.env import build_env
from runtime.adapters.hashing import (
    canonical_hash_dir,
    palette_entry_hash_for_producer,
)
from runtime.domain.models import (
    PALETTE_ARTIFACT_NAMES,
    PaletteEntry,
    PaletteUnavailableError,
)
from runtime.ports.color_scheme_generator import IColorSchemeGenerator

logger = logging.getLogger(__name__)

#: Env selector for the palette producer (composition roots read this).
PALETTE_PRODUCER_ENV: Final[str] = "RUNTIME__PALETTE__PRODUCER"

#: Env selector for tpr's generation engine (default: ``native``).
TPR_ENGINE_ENV: Final[str] = "RUNTIME__PALETTE__TPR_ENGINE"
TPR_DEFAULT_ENGINE: Final[str] = "native"

#: tpr's runtime mode. Container is the production default: the engine images
#: ship ``tpr-native``/``matugen``, so a fresh host needs no host helper.
TPR_DEFAULT_RUNTIME: Final[str] = "container"
VALID_TPR_RUNTIMES: Final[tuple[str, ...]] = ("local", "container")

#: Pinned fallback when ``tpr --version`` is unavailable (matches contracts
#: v0.4.0's tpr record era).
TPR_VERSION_FALLBACK: Final[str] = "v0.1.0"

#: ``tpr --version`` is a metadata probe, not a generation run.
_VERSION_TIMEOUT_SECONDS: Final[int] = 10

#: Contracts record timeout for tpr is 60 s, but the runtime keeps its own
#: graceful-cleanup convention (330 s) so a containerized tpr can tear down.
DEFAULT_TIMEOUT_SECONDS_TPR: Final[int] = DEFAULT_TIMEOUT_SECONDS


def _probe_version(tpr_bin: str) -> str | None:
    """Return the last token of ``tpr --version``, or ``None`` on failure."""
    try:
        result = subprocess.run(
            [tpr_bin, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
            env=build_env({}),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    tokens = (result.stdout or "").strip().split()
    return tokens[-1] if tokens else None


def _validate_tpr_bin(tpr_bin: str | None) -> str | None:
    if tpr_bin is None:
        return None
    if tpr_bin == "":
        raise ValueError("tpr_bin cannot be empty string (use None for default 'tpr')")
    # Reject shell metachars even though shell=False — defense in depth
    if any(c in tpr_bin for c in ";|&`$"):
        raise ValueError(f"tpr_bin contains shell metacharacter: {tpr_bin!r}")
    return tpr_bin


class TprAdapter(IColorSchemeGenerator):
    """Adapter that invokes ``tpr generate`` with the runtime's env override."""

    producer_name: str = "tpr"

    DEFAULT_TIMEOUT: int = DEFAULT_TIMEOUT_SECONDS_TPR

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        tpr_bin: str | None = None,
        templates_dir: Path | None = None,
        engine: str | None = None,
        runtime: str | None = None,
        settings_path: Path | None = None,
        version: str | None = None,
    ) -> None:
        if not isinstance(timeout, int):
            raise ValueError(f"timeout must be int, got {type(timeout).__name__}")
        if timeout <= 0:
            raise ValueError(f"timeout must be > 0, got {timeout}")
        self._timeout = timeout
        self._tpr_bin = _validate_tpr_bin(tpr_bin)
        self._templates_dir = templates_dir
        self._engine = (engine or TPR_DEFAULT_ENGINE).strip() or TPR_DEFAULT_ENGINE
        resolved_runtime = (runtime or TPR_DEFAULT_RUNTIME).strip() or TPR_DEFAULT_RUNTIME
        if resolved_runtime not in VALID_TPR_RUNTIMES:
            accepted = "|".join(VALID_TPR_RUNTIMES)
            raise ValueError(f"tpr runtime must be one of {accepted}, got {resolved_runtime!r}")
        self._runtime = resolved_runtime
        self._settings_path = settings_path
        self._version = version
        self._resolved_version: str | None = version

    @property
    def templates_dir(self) -> Path | None:
        """The spine-resolved tpr templates dir (``None`` when not supplied)."""
        return self._templates_dir

    @property
    def engine(self) -> str:
        return self._engine

    @property
    def runtime(self) -> str:
        """Resolved tpr runtime mode (``container`` unless explicitly local)."""
        return self._runtime

    @property
    def settings_path(self) -> Path | None:
        """Provisioned tpr settings pinned into the child env, when present."""
        return self._settings_path

    def version(self) -> str:
        """``tpr --version``'s last token, else the pinned fallback."""
        if self._resolved_version is not None:
            return self._resolved_version
        probed = _probe_version(self._tpr_bin or "tpr")
        self._resolved_version = probed or TPR_VERSION_FALLBACK
        return self._resolved_version

    def _resolve_templates_dir(self) -> Path:
        """Return the supplied templates dir, failing loudly when absent."""
        templates_dir = self._templates_dir
        if templates_dir is None:
            raise FileNotFoundError(
                "TPR templates dir not provided: TprAdapter was constructed without "
                "templates_dir. Resolve it from the install spine at "
                "config/theme-producer/templates and pass templates_dir=..."
            )
        return templates_dir

    def _resolve_template_set_hash(self) -> str:
        templates_dir = self._templates_dir
        if templates_dir is None:
            raise FileNotFoundError(
                "TPR templates dir not provided: TprAdapter was constructed without "
                "templates_dir. Resolve it from the install spine at "
                "config/theme-producer/templates and pass templates_dir=..."
            )
        if not templates_dir.exists():
            raise FileNotFoundError(f"TPR templates dir not found: {templates_dir}")
        if not templates_dir.is_dir():
            raise NotADirectoryError(f"TPR templates dir is not a directory: {templates_dir}")
        try:
            if not any(templates_dir.iterdir()):
                raise FileNotFoundError(f"TPR templates dir is empty: {templates_dir}")
        except OSError as exc:
            raise RuntimeError(f"cannot read templates dir {templates_dir}: {exc}") from exc
        try:
            template_set_hash = canonical_hash_dir(templates_dir)
        except OSError as exc:
            raise RuntimeError(f"cannot hash templates dir {templates_dir}: {exc}") from exc
        return template_set_hash

    def template_set_hash(self) -> str:
        """Canonical hash of the tpr template set (same value ``generate`` uses)."""
        return self._resolve_template_set_hash()

    def expected_entry_hash(self, wallpaper_hash: str) -> str:
        """TPR's palette entry key, seeded with producer/engine/version."""
        return palette_entry_hash_for_producer(
            self.producer_name,
            wallpaper_hash,
            self.template_set_hash(),
            engine=self._engine,
            version=self.version(),
        )

    def is_available(self) -> bool:
        """Return True if the ``tpr`` binary is on PATH and executable."""
        bin_name = self._tpr_bin or "tpr"
        if os.path.sep in bin_name or "\\" in bin_name:
            path = Path(bin_name)
            try:
                return path.is_file() and os.access(path, os.X_OK)
            except OSError:
                return False
        found = shutil.which(bin_name)
        if found is None:
            return False
        try:
            return os.access(found, os.X_OK)
        except OSError:
            return True

    def generate(self, wallpaper_path: Path, output_dir: Path) -> PaletteEntry:
        """Generate the eight consumer artifacts via ``tpr generate``.

        ``tpr`` runs against a private staging directory under
        ``output_dir.parent``; only ``PALETTE_ARTIFACT_NAMES`` are copied into
        ``output_dir``. ``theme.json`` and any other producer-side output are
        intentionally not part of the palette entry.
        """
        wallpaper_hash = _validate_wallpaper(wallpaper_path)
        template_set_hash = self._resolve_template_set_hash()
        entry_hash = self.expected_entry_hash(wallpaper_hash)
        _validate_output_dir(output_dir, entry_hash)

        try:
            output_dir.parent.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise RuntimeError(f"output_dir parent exists as file: {output_dir.parent}") from exc
        except OSError as exc:
            raise RuntimeError(
                f"cannot create output_dir parent {output_dir.parent}: {exc}"
            ) from exc
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise RuntimeError(f"output_dir exists as file, not dir: {output_dir}") from exc
        except OSError as exc:
            raise RuntimeError(f"cannot create output_dir {output_dir}: {exc}") from exc

        with tempfile.TemporaryDirectory(prefix=".tpr-", dir=output_dir.parent) as raw_tmp:
            producer_dir = Path(raw_tmp)
            overrides = {
                "THEME__OUTPUT__DIRECTORY": str(producer_dir),
                "THEME__OUTPUT__OVERWRITE": "true",
                "THEME__GENERATION__ENGINE": self._engine,
                "THEME__RUNTIME__MODE": self._runtime,
                # Pin the SAME catalog the adapter hashes; CWD discovery must
                # never select an unrelated templates/ tree.
                "THEME__TEMPLATES__DIRECTORY": str(self._resolve_templates_dir()),
            }
            if self._settings_path is not None and self._settings_path.is_file():
                # Provisioned tpr settings own the container image tag/engine.
                overrides["THEME_CONFIG_FILE_PATH"] = str(self._settings_path)
            env = build_env(overrides)
            bin_name = self._tpr_bin or "tpr"
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

            try:
                result = _run_csg(args, env, self._timeout)
            except FileNotFoundError as exc:
                raise FileNotFoundError("tpr not on PATH") from exc
            except PermissionError as exc:
                raise FileNotFoundError("tpr not on PATH (permission denied)") from exc
            except OSError as exc:
                if exc.errno == errno.E2BIG:
                    raise RuntimeError(f"tpr generate env too large (E2BIG): {exc}") from exc
                if exc.errno == errno.ENOENT:
                    raise FileNotFoundError("tpr not on PATH") from exc
                raise RuntimeError(f"tpr generate failed to spawn: {exc}") from exc

            if result.timed_out:
                missing, not_files = _inspect_artifacts(producer_dir)
                if not missing and not not_files:
                    logger.warning(
                        "tpr generate exceeded %ss (elapsed %.1fs, terminated_by=%s) "
                        "but produced the complete 8-artifact palette; accepting it",
                        self._timeout,
                        result.elapsed_seconds,
                        result.termination,
                    )
                    self._copy_artifacts(producer_dir, output_dir)
                    return _build_palette_entry(
                        output_dir,
                        entry_hash=entry_hash,
                        wallpaper_hash=wallpaper_hash,
                        template_set_hash=template_set_hash,
                        producer=self.producer_name,
                    )
                detail_parts: list[str] = []
                if missing:
                    detail_parts.append("missing=" + ",".join(missing))
                if not_files:
                    detail_parts.append("not_files=" + ",".join(not_files))
                stderr_part = result.stderr[:2048]
                msg = (
                    f"tpr generate timed out after {self._timeout}s "
                    f"(elapsed {result.elapsed_seconds:.1f}s, "
                    f"terminated_by={result.termination}); incomplete tpr output: "
                    + "; ".join(detail_parts)
                )
                if stderr_part:
                    msg += f": {stderr_part}"
                raise PaletteUnavailableError(
                    msg,
                    wallpaper_path=str(wallpaper_path),
                    wallpaper_hash=wallpaper_hash,
                    tool=self.producer_name,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=stderr_part,
                    elapsed_seconds=result.elapsed_seconds,
                    timed_out=True,
                    termination=result.termination,
                    missing_artifacts=tuple(missing + not_files),
                    hint=(
                        "tpr timed out and the palette artifact set is incomplete; "
                        "check tpr/engine logs, then re-set the wallpaper"
                    ),
                )

            if result.returncode != 0:
                if result.returncode is not None and result.returncode < 0:
                    sig = -result.returncode
                    try:
                        sig_name = signal.Signals(sig).name
                    except Exception:
                        sig_name = str(sig)
                    stderr = result.stderr[:2048]
                    stdout_part = result.stdout[:500]
                    raise PaletteUnavailableError(
                        f"tpr killed by signal {sig_name} ({sig}): {stderr} "
                        f"stdout:{stdout_part}",
                        wallpaper_path=str(wallpaper_path),
                        wallpaper_hash=wallpaper_hash,
                        tool=self.producer_name,
                        returncode=result.returncode,
                        stdout=result.stdout,
                        stderr=stderr,
                        elapsed_seconds=result.elapsed_seconds,
                        termination=sig_name,
                    )
                stderr = result.stderr[:2048]
                stdout = result.stdout[:500]
                raise PaletteUnavailableError(
                    f"tpr generate failed (exit {result.returncode}): {stderr} "
                    f"stdout:{stdout}",
                    wallpaper_path=str(wallpaper_path),
                    wallpaper_hash=wallpaper_hash,
                    tool=self.producer_name,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=stderr,
                    elapsed_seconds=result.elapsed_seconds,
                )

            missing, not_files = _inspect_artifacts(producer_dir)
            if missing or not_files:
                raise _artifact_failure(
                    producer=self.producer_name,
                    wallpaper_path=wallpaper_path,
                    wallpaper_hash=wallpaper_hash,
                    artifact_dir=producer_dir,
                    missing=missing,
                    not_files=not_files,
                    result=result,
                    hint=(
                        "tpr exited successfully but its artifact set is incomplete; "
                        "check the pinned templates dir and tpr logs, then re-set the wallpaper"
                    ),
                )

            self._copy_artifacts(producer_dir, output_dir)
            return _build_palette_entry(
                output_dir,
                entry_hash=entry_hash,
                wallpaper_hash=wallpaper_hash,
                template_set_hash=template_set_hash,
                producer=self.producer_name,
            )

    @staticmethod
    def _copy_artifacts(producer_dir: Path, output_dir: Path) -> None:
        """Copy exactly the eight contract artifacts; ignore ``theme.json``."""
        for name in PALETTE_ARTIFACT_NAMES:
            shutil.copyfile(producer_dir / name, output_dir / name)
