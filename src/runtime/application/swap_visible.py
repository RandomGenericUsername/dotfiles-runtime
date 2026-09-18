"""Visible-swap use case — phase 1 of the visible-first ``wallpaper set``.

Implements D1 (openspec ``wallpaper-set-visible-first``): import the
wallpaper into the cache, persist a wallpaper-only ``current.json``
(``palette``/``effects``/``icons`` null — schema-valid per
``contracts/schemas/current.schema.json``, crash-safe: a kill after this
phase reconverges via the existing reconcile), repoint ONLY the
wallpaper symlinks, and reload hyprpaper so pixels change before any
derivation runs.

Scope boundary:
- No derivation (no CSG/WEG/ITR): theming is ``ApplyWallpaperUseCase``'s
  derivation-only duty followed by the unchanged reconcile converge.
- No history append: the phase-2 reconcile owns the single
  ``trigger="set"`` line (``suppress_history`` threads through as before).
- No consumer-pointer repoint: spine pointers follow the palette in
  phase 2; the swap touches ``current/wallpaper-*.png`` (+ the
  ``wallpaper.png`` alias) only.

Architecture:
- Lives in ``application/`` (use-case layer) per AD-1, AD-13.
- Orchestrates injected ports and the injected ``CacheSeeder`` adapter;
  constructs no concrete adapters itself (composition root wires them).
- No raw ``os``/``json`` FS I/O in the use case (delegated to adapters).
- Failure policy (D5): any failure here means nothing changed on screen
  (hyprpaper never reloaded) — propagate loudly; the caller emits
  ``error`` with the pending hash. Post-visible failures belong to phase
  2 and never revert the wallpaper.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from runtime.adapters.hashing import hash_file
from runtime.adapters.seeder import CacheSeeder
from runtime.application.monitors import preserve_monitors
from runtime.domain.models import DEFAULT_MONITOR, DesktopState, WallpaperEntry
from runtime.ports.desktop_reloader import IDesktopReloader
from runtime.ports.monitor_source import IMonitorSource
from runtime.ports.seed_mutex import ISeedMutex
from runtime.ports.state_repository import IStateRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SwapVisibleResult:
    """Outcome of one ``SwapVisibleUseCase.run`` invocation."""

    wallpaper_hash: str
    wallpaper_path: Path
    repointed: list[Path]
    state: DesktopState


class SwapVisibleUseCase:
    """Swap the visible wallpaper before any derivation runs.

    Steps:
    1. Resolve + validate the input (absolute, existing regular file)
    2. Load current state (fail-fast corrupt-state guard, mirrors apply)
    3. Hash the wallpaper and import it into the cache via the seeder
       (copy policy — the cache owns its bytes for user-supplied files;
       content-verified; idempotent on re-entry)
    4. Hold the state mutex (blocking) around the read-modify-write of
       ``current.json`` — monitors preserved with ``source_hash``
       updated (AD-18) via the shared ``preserve_monitors`` helper —
       then repoint ONLY wallpaper symlinks and reload hyprpaper.

    Constructor receives ports, the injected ``CacheSeeder`` adapter, the
    state mutex, and the hyprpaper reloader (dependency inversion).
    """

    def __init__(
        self,
        state_repo: IStateRepository,
        state_root: Path,
        seeder: CacheSeeder,
        mutex: ISeedMutex,
        hyprpaper: IDesktopReloader,
        monitor_source: IMonitorSource | None = None,
    ) -> None:
        self._state_repo = state_repo
        self._state_root = state_root
        self._seeder = seeder
        self._mutex = mutex
        self._hyprpaper = hyprpaper
        self._monitor_source = monitor_source

    def run(self, image_path: Path) -> SwapVisibleResult:
        """Swap the visible wallpaper: import → persist → repoint → reload.

        Raises:
            ValueError: if the input path is missing or not a regular
                file; or propagated from a corrupt current.json
            RuntimeError: if the wallpaper cache entry is unusable, the
                hyprpaper reload fails, or the cache holds content that
                contradicts its hash
            OSError: on filesystem failures (unreadable input, etc.)
        """
        img = self._validate_input(image_path)

        # Corrupt-state guard (fail fast, mirrors apply): a
        # ValueError/RuntimeError from a corrupt store propagates loudly.
        self._state_repo.load_current()

        wallpaper_hash = hash_file(img)

        # Wallpaper layer: import into the cache (copy policy — cache owns
        # its bytes; idempotent, content-verified).
        cached = self._seeder.import_wallpaper(img, wallpaper_hash, source_mutable=True)

        with self._mutex.hold(blocking=True):
            existing = self._state_repo.load_current()
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            wallpaper_entry = WallpaperEntry(
                hash_algorithm="sha256",
                kind="wallpaper",
                content_hash=wallpaper_hash,
                source_path=str(img),
                imported_at=now,
            )
            detected = (
                self._monitor_source.detect_monitors() if self._monitor_source is not None else []
            )
            monitors = preserve_monitors(existing, wallpaper_hash, detected)
            state = DesktopState(
                schema_version=2,
                wallpaper=wallpaper_entry,
                monitors=monitors,
                palette=None,
                effects=None,
                icons=None,
                applied_at=now,
            )
            self._state_repo.save(state)

            if not cached.is_file():
                raise RuntimeError(f"wallpaper cache entry {wallpaper_hash} missing: {cached}")
            monitor_names = list(monitors) or [DEFAULT_MONITOR]
            repointed = self._seeder.repoint_current_symlinks(
                wallpaper_target=cached,
                monitor_names=monitor_names,
                palette_entry_hash=None,
                effects_entry_hash=None,
                icons_entry_hash=None,
            )

        try:
            hyprpaper_ok = self._hyprpaper.reload()
        except Exception as exc:
            raise RuntimeError(f"visible swap failed: hyprpaper reload raised: {exc}") from exc
        if not hyprpaper_ok:
            raise RuntimeError("visible swap failed: hyprpaper reload reported failure")
        return SwapVisibleResult(
            wallpaper_hash=wallpaper_hash,
            wallpaper_path=cached,
            repointed=repointed,
            state=state,
        )

    @staticmethod
    def _validate_input(image_path: Path) -> Path:
        """Resolve the input to an absolute path and validate it.

        Same contract as ``ApplyWallpaperUseCase._validate_input``: rejects
        missing files, directories, and anything that is not a regular
        file with ``ValueError`` (the CLI maps this to a non-zero exit).
        """
        img = image_path.expanduser().resolve()
        if not img.exists():
            raise ValueError(f"wallpaper image not found: {img}")
        if not img.is_file():
            raise ValueError(f"wallpaper image must be a regular file, got: {img}")
        return img
