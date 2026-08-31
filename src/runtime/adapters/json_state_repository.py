"""JSON adapter for IStateRepository — persists current.json.

Implements AD-3 (JSON store) + AD-5 (state_root XDG) + AD-14 (domain purity).

current.json schema (shared-data-contract, schema_version 2):
  {schema_version: 2, wallpaper: {...}, monitors: {...},
   palette: {...}|null, effects: {...}|null, icons: {...}|null,
   applied_at: <ISO-8601 UTC Z>}

Atomicity contract (C4):
  Write to sibling tmp (current.json.tmp.<pid>-<8hex>) then os.replace.
  POSIX atomic on same filesystem. tmp cleaned in finally.

Projection contract (C1):
  current.json stores only hash+generated_at for palette/effects/icons.
  Full input_template_hash/artifact_hashes live in cache/<layer>/<hash>/meta.json.
  On load, reconstruct minimal entries with SENTINEL="0"*64 placeholder.

Schema-version guard (E3):
  Validate schema_version == 2 immediately after json.loads, before any field.
  monitors absent tolerance (monitors={}) only when schema_version == 2.

Symlink TOCTOU (E1):
  Check is_symlink() before exists() to avoid symlink attack.

References:
  - AD-1 Hexagonal: adapters own FS I/O
  - AD-3 JSON store: deterministic sort_keys + indent
  - AD-4 history must-not-lose: NOT in this story (Story 3.1)
  - AD-5 state_root: XDG_STATE_HOME or ~/.local/state
  - AD-6 symlinks lead: recovery reads current.json
  - AD-8 SHA-256: hash_algorithm literal "sha256"
  - AD-14 domain purity: domain stays pure, adapters own os/json/pathlib/uuid
  - AD-15 cross-package boundary: never import config_assembler_engine
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from runtime.adapters.hashing import HASH_ALGORITHM, _validate_hex64
from runtime.domain.models import (
    BackendType,
    DesktopState,
    EffectsEntry,
    FitMode,
    IconsEntry,
    MonitorWallpaperConfig,
    PaletteArtifacts,
    PaletteEntry,
    WallpaperEntry,
)
from runtime.ports.state_repository import IStateRepository

if HASH_ALGORITHM != "sha256":
    raise AssertionError(f"HASH_ALGORITHM must be 'sha256', got {HASH_ALGORITHM!r}")

SENTINEL_HASH: Final[str] = "0" * 64  # placeholder; hydrated from cache/<layer>/<hash>/meta.json


def _now_iso_z() -> str:
    """Current UTC time as strict ISO-8601 ending with Z."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _validate_iso_z(name: str, value: str) -> None:
    """Validate strict ISO-8601 UTC timestamp ending with Z."""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be ISO-8601 UTC ending with 'Z', got {value!r}")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"{name} is not valid ISO-8601: {value!r}") from e


class JsonStateRepository(IStateRepository):
    """JSON adapter for IStateRepository — persists current.json atomically.

    state_root defaults to XDG_STATE_HOME/dotfiles (AD-5).
    Constructor allows injection for tests.
    """

    def __init__(
        self,
        state_root: Path | None = None,
        current_json: Path | None = None,
    ) -> None:
        if state_root is None:
            xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
            state_root = Path(xdg) / "dotfiles"

        # Validate state_root safety
        state_str = str(state_root)
        if "\x00" in state_str:
            raise ValueError(f"state_root must not contain null bytes: {state_root!r}")
        if len(state_str) >= 4096:
            raise ValueError(f"state_root path too long ({len(state_str)} >= 4096): {state_root!r}")
        if ".." in state_root.parts:
            raise ValueError(f"state_root must not contain '..' traversal: {state_root!r}")

        self.state_root: Path = state_root
        self._path: Path = current_json if current_json is not None else state_root / "current.json"

    def save(self, state: DesktopState) -> None:
        """Atomically persist current state via tmp + os.replace."""
        data = self._state_to_dict(state)
        self._validate_save(data)
        self._atomic_write(self._path, data)

    def load_current(self) -> DesktopState | None:
        """Load current state. Returns None on first run (no current.json).

        Raises ValueError on schema mismatch, symlink, or invalid JSON.
        Returns None ONLY for truly absent file (non-symlink, not exists).
        """
        # Symlink guard first (TOCTOU)
        if self._path.is_symlink():
            raise ValueError(f"current.json is not a regular file (symlink): {self._path}")

        # Fast-path absent
        if not self._path.exists():
            return None

        # Guard: must be regular file
        if not self._path.is_file():
            raise ValueError(f"current.json is not a regular file: {self._path}")

        # Read and parse JSON
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as e:
            raise RuntimeError(f"failed to read current.json: {e}") from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"current.json is not valid JSON: {e}") from e

        return self._dict_to_state(data)

    def _state_to_dict(self, state: DesktopState) -> dict[str, Any]:
        """Serialize DesktopState to dict per shared-data-contract."""
        wallpaper_dict: dict[str, Any] = {
            "hash": state.wallpaper.content_hash,
            "source_path": state.wallpaper.source_path,
            "applied_at": state.wallpaper.imported_at,
        }

        monitors_dict: dict[str, dict[str, Any]] = {}
        for name, cfg in state.monitors.items():
            monitors_dict[name] = {
                "backend": cfg.backend.value,
                "source_hash": cfg.source_hash,
                "fit_mode": cfg.fit_mode.value,
                "mpv_options": cfg.mpv_options,
                "ipc_socket": cfg.ipc_socket,
            }

        def _entry_projection(
            entry: PaletteEntry | EffectsEntry | IconsEntry | None,
        ) -> dict[str, str] | None:
            """Project entry to hash+generated_at only."""
            if entry is None:
                return None
            return {"hash": entry.entry_hash, "generated_at": entry.generated_at}

        return {
            "schema_version": 2,
            "wallpaper": wallpaper_dict,
            "monitors": monitors_dict,
            "palette": _entry_projection(state.palette),
            "effects": _entry_projection(state.effects),
            "icons": _entry_projection(state.icons),
            "applied_at": state.applied_at,
        }

    def _validate_save(self, data: dict[str, Any]) -> None:
        """Validate data before write — raises ValueError on any issue."""
        # schema_version must be literal 2
        if data.get("schema_version") != 2:
            raise ValueError(f"schema_version must be 2, got {data.get('schema_version')!r}")

        # wallpaper validation
        w = data.get("wallpaper")
        if not isinstance(w, dict):
            raise ValueError("wallpaper must be a dict")
        _validate_hex64("wallpaper.hash", w["hash"])
        if not isinstance(w.get("source_path"), str):
            raise ValueError("wallpaper.source_path must be a string")
        _validate_iso_z("wallpaper.applied_at", w["applied_at"])

        # monitors validation
        monitors = data.get("monitors")
        if not isinstance(monitors, dict):
            raise ValueError("monitors must be a dict")
        for name, cfg in monitors.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"monitor name must be non-empty string, got {name!r}")
            if not isinstance(cfg, dict):
                raise ValueError(f"monitor {name!r} config must be a dict")
            if cfg.get("backend") not in BackendType.__members__.values():
                raise ValueError(f"monitor {name!r} backend invalid: {cfg.get('backend')!r}")
            _validate_hex64(f"monitor {name!r}.source_hash", cfg["source_hash"])
            if cfg.get("fit_mode") not in FitMode.__members__.values():
                raise ValueError(f"monitor {name!r} fit_mode invalid: {cfg.get('fit_mode')!r}")
            # mpv_options/ipc_socket only for mpvpaper
            backend = BackendType(cfg["backend"])
            if backend != BackendType.mpvpaper:
                if cfg.get("mpv_options") is not None or cfg.get("ipc_socket") is not None:
                    raise ValueError(
                        f"monitor {name!r}: mpv_options/ipc_socket only valid for mpvpaper"
                    )

        # palette/effects/icons projection validation
        for key in ("palette", "effects", "icons"):
            entry = data.get(key)
            if entry is not None:
                if not isinstance(entry, dict):
                    raise ValueError(f"{key} must be dict or null")
                _validate_hex64(f"{key}.hash", entry["hash"])
                _validate_iso_z(f"{key}.generated_at", entry["generated_at"])

        # applied_at validation
        _validate_iso_z("applied_at", data["applied_at"])

    def _atomic_write(self, path: Path, data: dict[str, Any]) -> None:
        """Write JSON atomically via sibling tmp + os.replace."""
        # Ensure parent directory exists
        path.parent.mkdir(parents=True, exist_ok=True)

        # Sibling tmp in same dir (ensures same filesystem for atomic os.replace)
        tmp_name = f"{path.name}.tmp.{os.getpid()}-{uuid.uuid4().hex[:8]}"
        tmp = path.parent / tmp_name

        try:
            # Write deterministic JSON: sort_keys + indent=2 + trailing newline
            content = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            tmp.write_text(content, encoding="utf-8")
            # POSIX atomic replace
            os.replace(tmp, path)
        finally:
            # Best-effort cleanup of tmp on failure
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _dict_to_state(self, data: dict[str, Any]) -> DesktopState:
        """Deserialize dict to DesktopState with validation."""
        # Schema version check FIRST (E3 fix)
        v = data.get("schema_version")
        if v != 2:
            raise ValueError(f"unsupported schema_version: {v!r}, expected 2")

        # wallpaper validation
        try:
            w = data["wallpaper"]
        except KeyError as e:
            raise ValueError(f"current.json missing required field: {e.args[0]!r}") from e
        _validate_hex64("wallpaper.hash", w["hash"])
        if not isinstance(w.get("source_path"), str):
            raise ValueError("wallpaper.source_path must be a string")
        _validate_iso_z("wallpaper.applied_at", w["applied_at"])
        wallpaper = WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=w["hash"],
            source_path=w["source_path"],
            imported_at=w["applied_at"],
        )

        # monitors — tolerate absent (v1 legacy, schema_version already verified 2)
        monitors_raw = data.get("monitors")
        if monitors_raw is None:
            # v1 migration deferred to SeedCacheUseCase (1.11)
            monitors_raw = {}
        if not isinstance(monitors_raw, dict):
            raise ValueError("monitors must be a dict")

        monitors: dict[str, MonitorWallpaperConfig] = {}
        for name, cfg in monitors_raw.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"monitor name must be non-empty string, got {name!r}")
            if not isinstance(cfg, dict):
                raise ValueError(f"monitor {name!r} config must be a dict")
            backend = BackendType(cfg["backend"])
            _validate_hex64(f"monitor {name!r}.source_hash", cfg["source_hash"])
            fit_mode = FitMode(cfg["fit_mode"])
            mpv_options = cfg.get("mpv_options")
            ipc_socket = cfg.get("ipc_socket")
            # mpv guard
            if backend != BackendType.mpvpaper:
                if mpv_options is not None or ipc_socket is not None:
                    raise ValueError(
                        f"monitor {name!r}: mpv_options/ipc_socket only valid for mpvpaper"
                    )
            monitors[name] = MonitorWallpaperConfig(
                backend=backend,
                source_hash=cfg["source_hash"],
                fit_mode=fit_mode,
                mpv_options=mpv_options,
                ipc_socket=ipc_socket,
            )

        # palette/effects/icons projection reconstruction
        def _load_projection(
            key: str,
            source_wallpaper_hash: str,
            extra_fields: dict[str, str] | None = None,
        ) -> PaletteEntry | EffectsEntry | IconsEntry | None:
            raw = data.get(key)
            if raw is None:
                return None
            if not isinstance(raw, dict):
                raise ValueError(f"{key} must be dict or null")
            _validate_hex64(f"{key}.hash", raw["hash"])
            _validate_iso_z(f"{key}.generated_at", raw["generated_at"])
            # Reconstruct with SENTINEL hashes (full derivation lives in cache meta.json)
            if key == "palette":
                artifact_hashes: PaletteArtifacts = {
                    "colors_yaml": SENTINEL_HASH,
                    "colors_conf": SENTINEL_HASH,
                    "colors_gtk_css": SENTINEL_HASH,
                }
                return PaletteEntry(
                    hash_algorithm="sha256",
                    kind="palette",
                    entry_hash=raw["hash"],
                    source_wallpaper_hash=source_wallpaper_hash,
                    input_template_hash=SENTINEL_HASH,
                    artifact_hashes=artifact_hashes,
                    generated_at=raw["generated_at"],
                )
            elif key == "effects":
                return EffectsEntry(
                    hash_algorithm="sha256",
                    kind="effects",
                    entry_hash=raw["hash"],
                    source_wallpaper_hash=source_wallpaper_hash,
                    input_catalog_hash=SENTINEL_HASH,
                    artifact_hashes={},
                    generated_at=raw["generated_at"],
                )
            elif key == "icons":
                palette_hash = extra_fields["palette_hash"] if extra_fields else SENTINEL_HASH
                return IconsEntry(
                    hash_algorithm="sha256",
                    kind="icons",
                    entry_hash=raw["hash"],
                    source_palette_hash=palette_hash,
                    input_templates_hash=SENTINEL_HASH,
                    input_mappings_hash=SENTINEL_HASH,
                    artifact_hashes={},
                    generated_at=raw["generated_at"],
                )
            raise ValueError(f"unknown projection key: {key!r}")

        palette_result = _load_projection("palette", wallpaper.content_hash)
        effects_result = _load_projection("effects", wallpaper.content_hash)
        palette_hash = palette_result.entry_hash if palette_result else SENTINEL_HASH
        icons_result = _load_projection(
            "icons", wallpaper.content_hash, {"palette_hash": palette_hash}
        )

        # Type narrowing for DesktopState construction
        palette: PaletteEntry | None = (
            palette_result if isinstance(palette_result, PaletteEntry) else None
        )
        effects: EffectsEntry | None = (
            effects_result if isinstance(effects_result, EffectsEntry) else None
        )
        icons: IconsEntry | None = icons_result if isinstance(icons_result, IconsEntry) else None

        # applied_at validation
        try:
            applied_at = data["applied_at"]
        except KeyError as e:
            raise ValueError(f"current.json missing required field: {e.args[0]!r}") from e
        _validate_iso_z("applied_at", applied_at)

        return DesktopState(
            schema_version=2,
            wallpaper=wallpaper,
            monitors=monitors,
            palette=palette,
            effects=effects,
            icons=icons,
            applied_at=applied_at,
        )
