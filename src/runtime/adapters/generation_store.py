"""v3 committed generation store (contracts v0.9.0 ``generations.md``).

One immutable generation directory, one atomic ``state/current`` symlink, and
one compatibility ``current.json`` view. Everything the runtime publishes for
the desktop lives under ``state/``; this adapter never writes the install spine
or an app tag.

Layout (frozen):

    generations/<genHash>/
      manifest.json                  written last; the self-check
      palette -> ../../cache/palettes/<paletteHash>
      effects -> ../../cache/effects/<effectsHash>
      icons   -> ../../cache/icons/<iconsHash>
      wallpaper/wallpaper.png -> ../../../cache/wallpapers/<wallpaperHash>/wallpaper.png
      appearance/tokens.json
      appearance/<configDir>.css
      colors.* -> palette/colors.*   compatibility aliases inside the generation
      wallpaper.png -> wallpaper/wallpaper.png
      wallpaper-<monitor>.png -> wallpaper/wallpaper.png
    current -> generations/<genHash>
    appearance-ledger.json
    gc-pins.json
    converge.lock

The commit transaction is: stage (sibling ``.staging-*``), write every file,
write ``manifest.json`` last, fsync files + directories, publish the immutable
directory with one ``rename``, then swap ``current`` with one symlink
``rename``. A crash before either rename leaves the old ``current``; a crash
after the generation rename leaves a complete orphan that the next converge
reuses by hash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fastjsonschema

from runtime.adapters.contract_schemas import generation_manifest_validator
from runtime.adapters.hashing import appearance_generation_hash
from runtime.domain.host_defaults import HOST_MAP_VERSION, HOST_SOURCE
from runtime.domain.models import (
    AppearanceEntry,
    DesktopState,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CONVERGE_LOCK_FILENAME",
    "CURRENT_NAME",
    "GC_PINS_FILENAME",
    "GENERATIONS_DIR",
    "GenerationManifestError",
    "GenerationStore",
    "compute_generation_hash",
    "generation_intent_hash",
]

GENERATIONS_DIR = "generations"
CURRENT_NAME = "current"
MANIFEST_NAME = "manifest.json"
GC_PINS_FILENAME = "gc-pins.json"
CONVERGE_LOCK_FILENAME = "converge.lock"
STAGING_PREFIX = ".staging-"
MANIFEST_VERSION = 3
GC_PINS_VERSION = 1
DEFAULT_KEEP = 5
DEFAULT_GRACE_SECONDS = 24 * 60 * 60

#: Manifest layer keys are singular; cache directories are plural. GC must
#: map them explicitly or it protects nothing for wallpaper/palette.
_MANIFEST_LAYER_TO_CACHE_LAYER = {
    "wallpaper": "wallpapers",
    "palette": "palettes",
    "effects": "effects",
    "icons": "icons",
}

#: Cache layers swept by generation GC. ``appearance`` is composed (not a
#: manifest layer); its hash is recomputed from the manifest identity.
_SWEPT_CACHE_LAYERS = ("wallpapers", "palettes", "effects", "icons", "appearance")

_PALETTE_ALIASES = (
    "colors.conf",
    "colors.gtk.css",
    "colors.yaml",
    "colors.adw.css",
    "colors.sequences",
    "colors.rasi",
    "colors.kitty",
    "colors.btop.theme",
)

_HEX_DIGITS = frozenset("0123456789abcdef")


class GenerationManifestError(ValueError):
    """A generation directory or manifest failed its self-check."""


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX_DIGITS for c in value)


def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    _fsync_file(path)


def _symlink(link: Path, target: str) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    _fsync_dir(link.parent)


def _canonical_hash(payload: object) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def generation_intent_hash(
    *,
    wallpaper: str,
    palette: str,
    skin: str,
    mode: str,
    effects: str | None = None,
    icons: str | None = None,
    host: str | None = None,
) -> str:
    """Canonical hash of the resolved intent snapshot (stable, no timestamps).

    ``host`` is included only when a delegated host value participated. The
    daemon queue key deliberately never passes it: the host is read inside the
    converge lock and the executing job computes this generation intent.
    """
    payload: dict[str, str | None] = {
        "wallpaper": wallpaper,
        "palette": palette,
        "skin": skin,
        "mode": mode,
        "effects": effects,
        "icons": icons,
    }
    if host is not None:
        payload["host"] = host
    return _canonical_hash(payload)


def compute_generation_hash(
    *,
    layer_hashes: Mapping[str, str],
    skin_name: str,
    skin_hash: str,
    mode: str,
    appearance_hash: str,
    intent_hash: str,
    host: str | None = None,
) -> str:
    """Canonical generation identity; timestamps and executor are excluded."""
    payload: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "layers": dict(sorted(layer_hashes.items())),
        "skin": {"name": skin_name, "entryHash": skin_hash, "mode": mode},
        "mode": mode,
        "appearance": {"artifactHash": appearance_hash},
        "intentHash": intent_hash,
    }
    if host is not None:
        payload["host"] = host
    return _canonical_hash(payload)


@dataclass(frozen=True, slots=True)
class GenerationSpec:
    """Everything one converge commit needs; no filesystem object is embedded."""

    state: DesktopState
    appearance: AppearanceEntry
    mode: str
    skin_name: str
    skin_hash: str
    intent_hash: str
    monitor_names: tuple[str, ...]
    source_kind: str
    job_id: str | None = None
    created_at: str | None = None
    host_hash: str | None = None
    host_values: tuple[tuple[str, int | float], ...] = ()


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Published generation identity and the paths a caller reports."""

    generation_hash: str
    path: Path
    manifest: dict[str, Any]
    created: bool


class GenerationStore:
    """Own the generation directories, the commit pointer and GC metadata."""

    def __init__(
        self,
        state_root: Path,
        *,
        fault: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._state_root = state_root
        self._fault = fault
        self._clock = clock

    # -- paths ---------------------------------------------------------

    @property
    def generations_root(self) -> Path:
        return self._state_root / GENERATIONS_DIR

    @property
    def current_link(self) -> Path:
        return self._state_root / CURRENT_NAME

    @property
    def pins_path(self) -> Path:
        return self._state_root / GC_PINS_FILENAME

    def generation_path(self, generation_hash: str) -> Path:
        if not _is_hex64(generation_hash):
            raise GenerationManifestError(
                f"generation hash must be 64-char lowercase hex, got {generation_hash!r}"
            )
        return self.generations_root / generation_hash

    # -- reading -------------------------------------------------------

    def load_manifest(self, generation_hash: str) -> dict[str, Any] | None:
        """Read and validate one generation manifest, or ``None`` if invalid."""
        path = self.generation_path(generation_hash) / MANIFEST_NAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            self._validate_manifest(data, generation_hash)
        except GenerationManifestError as exc:
            logger.warning("generation: invalid manifest %s: %s", path, exc)
            return None
        return data

    def current_hash(self) -> str | None:
        """The committed generation hash through ``state/current``."""
        link = self.current_link
        if not link.is_symlink():
            return None
        try:
            raw = os.readlink(link)
        except OSError:
            return None
        name = Path(raw).name
        if not _is_hex64(name):
            return None
        return name

    def current_manifest(self) -> dict[str, Any] | None:
        generation = self.current_hash()
        if generation is None:
            return None
        return self.load_manifest(generation)

    def is_current(self, generation_hash: str) -> bool:
        return self.current_hash() == generation_hash

    def _maybe_fault(self, point: str) -> None:
        if self._fault is not None:
            self._fault(point)

    # -- staging / commit ---------------------------------------------

    def commit(self, spec: GenerationSpec) -> GenerationResult:
        """Stage, publish and point ``current`` at one generation.

        The caller holds ``converge.lock``. The only visible transition is the
        single ``current`` rename; the generation directory is immutable once
        published.
        """
        layer_hashes = self._layer_hashes(spec.state)
        generation_hash = compute_generation_hash(
            layer_hashes=layer_hashes,
            skin_name=spec.skin_name,
            skin_hash=spec.skin_hash,
            mode=spec.mode,
            appearance_hash=spec.appearance.appearance_hash,
            intent_hash=spec.intent_hash,
            host=spec.host_hash,
        )
        target = self.generation_path(generation_hash)
        created = False
        if target.is_dir():
            try:
                self.verify(generation_hash)
            except GenerationManifestError as exc:
                logger.warning(
                    "generation %s is complete but inconsistent (%s); rebuilding",
                    generation_hash[:12],
                    exc,
                )
                aside = self.generations_root / (
                    f".invalid-{generation_hash[:12]}-{uuid.uuid4().hex[:8]}"
                )
                os.rename(target, aside)
                shutil.rmtree(aside, ignore_errors=True)
                self._stage_and_publish(target, spec, layer_hashes, generation_hash)
                created = True
        else:
            self._stage_and_publish(target, spec, layer_hashes, generation_hash)
            created = True
        self._maybe_fault("published")
        self._commit_pointer(generation_hash)
        self._maybe_fault("committed")
        manifest = self.load_manifest(generation_hash)
        if manifest is None:
            raise GenerationManifestError(
                f"committed generation {generation_hash} has no valid manifest"
            )
        return GenerationResult(
            generation_hash=generation_hash,
            path=target,
            manifest=manifest,
            created=created or target.is_dir(),
        )

    def _stage_and_publish(
        self,
        target: Path,
        spec: GenerationSpec,
        layer_hashes: Mapping[str, str],
        generation_hash: str,
    ) -> None:
        staging = self.generations_root / (
            f"{STAGING_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"
        )
        self.generations_root.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=False, exist_ok=False)
        try:
            self._populate(staging, spec, layer_hashes, generation_hash)
            self._maybe_fault("staged")
            try:
                os.rename(staging, target)
            except OSError:
                if not target.is_dir():
                    raise
                # Lost a race: the winner's immutable generation is
                # authoritative and identical by content hash.
                shutil.rmtree(staging, ignore_errors=True)
            else:
                _fsync_dir(self.generations_root)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _layer_hashes(self, state: DesktopState) -> dict[str, str]:
        palette = state.palette
        if palette is None:
            raise GenerationManifestError("a generation requires a palette layer")
        layers = {
            "wallpaper": state.wallpaper.content_hash,
            "palette": palette.entry_hash,
        }
        if state.effects is not None:
            layers["effects"] = state.effects.entry_hash
        if state.icons is not None:
            layers["icons"] = state.icons.entry_hash
        return layers

    def _populate(
        self,
        staging: Path,
        spec: GenerationSpec,
        layer_hashes: Mapping[str, str],
        generation_hash: str,
    ) -> dict[str, Any]:
        state = spec.state
        palette = state.palette
        assert palette is not None
        palette_rel = f"../../cache/palettes/{palette.entry_hash}"
        _symlink(staging / "palette", palette_rel)

        if state.effects is not None:
            _symlink(
                staging / "effects",
                f"../../cache/effects/{state.effects.entry_hash}",
            )
        if state.icons is not None:
            _symlink(
                staging / "icons",
                f"../../cache/icons/{state.icons.entry_hash}",
            )

        wallpaper_dir = staging / "wallpaper"
        wallpaper_dir.mkdir()
        wallpaper_target = (
            f"../../../cache/wallpapers/{state.wallpaper.content_hash}/wallpaper.png"
        )
        _symlink(wallpaper_dir / "wallpaper.png", wallpaper_target)
        _fsync_dir(wallpaper_dir)

        for alias in _PALETTE_ALIASES:
            _symlink(staging / alias, f"palette/{alias}")
        _symlink(staging / "wallpaper.png", "wallpaper/wallpaper.png")
        for monitor in spec.monitor_names:
            _symlink(
                staging / f"wallpaper-{monitor}.png",
                "wallpaper/wallpaper.png",
            )

        appearance_dir = staging / "appearance"
        appearance_dir.mkdir()
        source_dir = self._state_root / "cache" / "appearance" / spec.appearance.appearance_hash
        if not source_dir.is_dir():
            raise GenerationManifestError(f"composed appearance entry missing: {source_dir}")
        tokens_source = source_dir / "tokens.json"
        if not tokens_source.is_file():
            raise GenerationManifestError(f"composed appearance tokens missing: {tokens_source}")
        shutil.copyfile(tokens_source, appearance_dir / "tokens.json")
        _fsync_file(appearance_dir / "tokens.json")
        targets: list[dict[str, str]] = []
        for config_dir in sorted(spec.appearance.config_dirs):
            source = source_dir / f"{config_dir}.css"
            if not source.is_file():
                raise GenerationManifestError(
                    f"composed appearance sheet missing for {config_dir!r}: {source}"
                )
            destination = appearance_dir / f"{config_dir}.css"
            shutil.copyfile(source, destination)
            _fsync_file(destination)
            targets.append(
                {
                    "configDir": config_dir,
                    "path": f"appearance/{config_dir}.css",
                }
            )
        _fsync_dir(appearance_dir)

        created_at = spec.created_at or _now_z()
        layers: dict[str, dict[str, str]] = {
            "wallpaper": {
                "entryHash": state.wallpaper.content_hash,
                "path": "wallpaper",
            },
            "palette": {"entryHash": palette.entry_hash, "path": "palette"},
        }
        if state.effects is not None:
            layers["effects"] = {"entryHash": state.effects.entry_hash, "path": "effects"}
        if state.icons is not None:
            layers["icons"] = {"entryHash": state.icons.entry_hash, "path": "icons"}

        intent: dict[str, str] = {
            "wallpaper": state.wallpaper.content_hash,
            "palette": palette.entry_hash,
            "skin": spec.skin_name,
            "mode": spec.mode,
        }
        if state.effects is not None:
            intent["effects"] = state.effects.entry_hash
        if state.icons is not None:
            intent["icons"] = state.icons.entry_hash
        if spec.host_hash is not None:
            intent["host"] = spec.host_hash

        source: dict[str, str] = {
            "kind": spec.source_kind,
            "intentHash": spec.intent_hash,
        }
        if spec.job_id is not None:
            source["jobId"] = spec.job_id

        manifest: dict[str, Any] = {
            "version": MANIFEST_VERSION,
            "generationHash": generation_hash,
            "created": {
                "at": created_at,
                "by": "dotfiles-runtime",
            },
            "committed": {
                "at": created_at,
            },
            "layers": layers,
            "skin": {
                "name": spec.skin_name,
                "entryHash": spec.skin_hash,
                "mode": spec.mode,
            },
            "appearance": {
                "tokensPath": "appearance/tokens.json",
                "targets": targets,
            },
            "mode": spec.mode,
            "intent": intent,
            "source": source,
        }
        if spec.host_hash is not None:
            manifest["host"] = {
                "source": HOST_SOURCE,
                "version": HOST_MAP_VERSION,
                "fingerprint": spec.host_hash,
                "defaults": {path: value for path, value in spec.host_values},
            }
        if spec.job_id is not None:
            manifest["committed"]["jobId"] = spec.job_id
        self._validate_manifest(manifest, generation_hash)
        # The manifest is written LAST: a torn staging directory is never a
        # generation even if its rename somehow completed.
        _write_text(
            staging / MANIFEST_NAME,
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )
        for child in staging.iterdir():
            if child.is_dir() and not child.is_symlink():
                _fsync_dir(child)
        _fsync_dir(staging)
        return manifest

    def _commit_pointer(self, generation_hash: str) -> None:
        state_root = self._state_root
        state_root.mkdir(parents=True, exist_ok=True)
        tmp = state_root / f".{CURRENT_NAME}.tmp.{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            # One-time migration: a v2 ``current/`` directory of family links
            # is moved aside so the v3 commit pointer can be installed. The
            # move is under converge.lock; no reader is promised the v2 shape
            # after the first v3 converge.
            if self.current_link.exists() and not self.current_link.is_symlink():
                os.rename(
                    self.current_link,
                    state_root / f".{CURRENT_NAME}.v2-{uuid.uuid4().hex[:8]}",
                )
            tmp.symlink_to(Path(GENERATIONS_DIR) / generation_hash)
            os.replace(str(tmp), str(self.current_link))
            _fsync_dir(state_root)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # -- validation ----------------------------------------------------

    def _validate_manifest(self, data: dict[str, Any], expected_hash: str) -> None:
        try:
            generation_manifest_validator()(data)
        except fastjsonschema.JsonSchemaValueException as exc:
            raise GenerationManifestError(f"manifest schema violation: {exc.message}") from exc
        if data.get("generationHash") != expected_hash:
            raise GenerationManifestError(
                "generationHash does not equal the generation directory basename"
            )
        if data.get("mode") != data.get("skin", {}).get("mode"):
            raise GenerationManifestError("skin.mode must equal manifest.mode")
        intent = data.get("intent", {})
        if intent.get("mode") != data.get("mode"):
            raise GenerationManifestError("intent.mode must equal manifest.mode")
        if intent.get("skin") != data.get("skin", {}).get("name"):
            raise GenerationManifestError("intent.skin must equal skin.name")
        host = data.get("host")
        if host is None:
            if "host" in intent:
                raise GenerationManifestError(
                    "intent.host must be absent when the generation has no host block"
                )
        elif intent.get("host") != host.get("fingerprint"):
            raise GenerationManifestError(
                "intent.host must equal host.fingerprint"
            )
        layers = data.get("layers", {})
        for name, value in layers.items():
            if value.get("path") != name:
                raise GenerationManifestError(
                    f"layer {name!r} path must be the generation-local {name!r}"
                )
            if name in intent and intent[name] != value.get("entryHash"):
                raise GenerationManifestError(
                    f"intent.{name} must equal layers.{name}.entryHash"
                )
        targets = data.get("appearance", {}).get("targets", [])
        seen: set[str] = set()
        for target in targets:
            config_dir = target.get("configDir")
            if config_dir in seen:
                raise GenerationManifestError(f"duplicate appearance configDir {config_dir!r}")
            seen.add(config_dir)
            if target.get("path") != f"appearance/{config_dir}.css":
                raise GenerationManifestError(
                    f"appearance target path for {config_dir!r} is not canonical"
                )

    def verify(self, generation_hash: str) -> dict[str, Any]:
        """Fail closed unless the generation is complete and self-consistent."""
        manifest = self.load_manifest(generation_hash)
        if manifest is None:
            raise GenerationManifestError(f"generation {generation_hash} has no valid manifest")
        root = self.generation_path(generation_hash)
        for name, layer in manifest["layers"].items():
            path = root / layer["path"]
            if not (path.exists() or path.is_symlink()):
                raise GenerationManifestError(
                    f"generation {generation_hash} layer {name!r} is missing: {path}"
                )
        for target in manifest["appearance"]["targets"]:
            path = root / target["path"]
            if not path.is_file():
                raise GenerationManifestError(
                    f"generation {generation_hash} appearance target missing: {path}"
                )
        tokens = root / manifest["appearance"]["tokensPath"]
        if not tokens.is_file():
            raise GenerationManifestError(
                f"generation {generation_hash} tokens missing: {tokens}"
            )
        for alias in _PALETTE_ALIASES:
            link = root / alias
            if not link.is_symlink() or os.readlink(link) != f"palette/{alias}":
                raise GenerationManifestError(
                    f"generation {generation_hash} compatibility alias is invalid: {link}"
                )
        wallpaper = root / "wallpaper.png"
        if not wallpaper.is_symlink() or os.readlink(wallpaper) != "wallpaper/wallpaper.png":
            raise GenerationManifestError(
                f"generation {generation_hash} wallpaper alias is invalid: {wallpaper}"
            )
        for link in root.glob("wallpaper-*.png"):
            if not link.is_symlink() or os.readlink(link) != "wallpaper/wallpaper.png":
                raise GenerationManifestError(
                    f"generation {generation_hash} monitor wallpaper alias is invalid: {link}"
                )
        return manifest

    # -- GC / pins -----------------------------------------------------

    def load_pins(self) -> dict[str, Any]:
        try:
            data = json.loads(self.pins_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return {"version": GC_PINS_VERSION, "generations": []}
        if not isinstance(data, dict) or data.get("version") != GC_PINS_VERSION:
            return {"version": GC_PINS_VERSION, "generations": []}
        entries = data.get("generations")
        if not isinstance(entries, list):
            return {"version": GC_PINS_VERSION, "generations": []}
        return data

    def pinned_hashes(self) -> frozenset[str]:
        return frozenset(
            entry["generationHash"]
            for entry in self.load_pins().get("generations", [])
            if isinstance(entry, dict) and _is_hex64(entry.get("generationHash"))
        )

    def add_pin(self, generation_hash: str, reason: str) -> None:
        if not _is_hex64(generation_hash):
            raise GenerationManifestError(
                f"pin hash must be 64-char lowercase hex, got {generation_hash!r}"
            )
        pins = self.load_pins()
        entries = [
            entry
            for entry in pins.get("generations", [])
            if isinstance(entry, dict) and entry.get("generationHash") != generation_hash
        ]
        entries.append(
            {
                "generationHash": generation_hash,
                "reason": reason,
                "addedAt": _now_z(),
            }
        )
        pins["version"] = GC_PINS_VERSION
        pins["generations"] = entries
        self._write_pins(pins)

    def remove_pin(self, generation_hash: str) -> bool:
        pins = self.load_pins()
        entries = [
            entry
            for entry in pins.get("generations", [])
            if isinstance(entry, dict) and entry.get("generationHash") != generation_hash
        ]
        if len(entries) == len(pins.get("generations", [])):
            return False
        pins["version"] = GC_PINS_VERSION
        pins["generations"] = entries
        self._write_pins(pins)
        return True

    def _write_pins(self, pins: dict[str, Any]) -> None:
        self.pins_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pins_path.parent / (
            f".{GC_PINS_FILENAME}.tmp.{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        try:
            tmp.write_text(
                json.dumps(pins, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _fsync_file(tmp)
            os.replace(str(tmp), str(self.pins_path))
            _fsync_dir(self.pins_path.parent)
        finally:
            tmp.unlink(missing_ok=True)

    def gc(
        self,
        *,
        keep: int = DEFAULT_KEEP,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
        ledger_generations: Iterable[str] = (),
        ledger_layers: Mapping[str, Iterable[str]] | None = None,
        acknowledged_generations: Iterable[str] = (),
        current_generation: str | None = None,
    ) -> dict[str, list[str]]:
        """Mark-and-sweep under the caller's ``converge.lock``.

        Retains every generation named by the current pointer (and the
        explicit post-commit ``current_generation``), the newest ``keep``,
        every pin, every ledger/acknowledged generation and every
        ledger-referenced cache layer. Cache protection is derived from each
        retained generation's manifest: singular manifest layer keys are
        mapped to their plural cache directories, and the composed appearance
        cache entry is recomputed from ``palette/skin/mode``. The newest entry
        per layer is also protected so a concurrent/pre-commit derive is never
        swept. Only unreferenced entries older than ``grace_seconds`` are
        removed.

        The caller holds ``converge.lock`` and must pass the just-committed
        generation as ``current_generation``. If a current generation is
        named but its manifest cannot be read, the cache sweep is skipped
        (fail closed) rather than risking a dangling ``current`` layer.
        """
        if keep < 0:
            raise ValueError(f"keep must be >= 0, got {keep}")
        if current_generation is not None and not _is_hex64(current_generation):
            raise GenerationManifestError(
                "current_generation must be 64-char lowercase hex, "
                f"got {current_generation!r}"
            )
        root = self.generations_root
        if not root.is_dir() or root.is_symlink():
            return {"generations": [], "cache": [], "staging": []}

        active = self.current_hash()
        current_hashes = {generation for generation in (active, current_generation) if generation}
        pinned = self.pinned_hashes()
        referenced = set(ledger_generations) | set(acknowledged_generations) | pinned
        referenced.update(current_hashes)

        now = self._clock()
        entries: list[tuple[Path, float]] = []
        staging_removed: list[str] = []
        for child in sorted(root.iterdir()):
            if child.name.startswith(STAGING_PREFIX):
                try:
                    age = now - child.stat().st_mtime
                except OSError:
                    continue
                if age >= grace_seconds:
                    shutil.rmtree(child, ignore_errors=True)
                    staging_removed.append(child.name)
                continue
            if not _is_hex64(child.name) or child.is_symlink() or not child.is_dir():
                continue
            try:
                entries.append((child, now - child.stat().st_mtime))
            except OSError:
                continue
        entries.sort(key=lambda pair: pair[1])
        newest = {path.name for path, _age in entries[:keep]}
        protected = referenced | newest

        removed_generations: list[str] = []
        for path, age in entries:
            if path.name in protected:
                continue
            if age < grace_seconds:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed_generations.append(path.name)

        retained_manifests: list[dict[str, Any]] = []
        retained_hashes: set[str] = set()
        for child, _age in entries:
            if child.name in removed_generations:
                continue
            manifest = self.load_manifest(child.name)
            if manifest is not None:
                retained_manifests.append(manifest)
                retained_hashes.add(child.name)
        # A current generation is authoritative even when it was outside the
        # sweep list above; its manifest must be part of the protection set.
        for generation in sorted(current_hashes):
            if generation in retained_hashes:
                continue
            manifest = self.load_manifest(generation)
            if manifest is not None:
                retained_manifests.append(manifest)
                retained_hashes.add(generation)

        cache_sweep_allowed = True
        for generation in sorted(current_hashes):
            if self.load_manifest(generation) is None:
                logger.warning(
                    "gc: current generation %s has no readable manifest; "
                    "refusing the cache sweep",
                    generation,
                )
                cache_sweep_allowed = False
        if not cache_sweep_allowed:
            return {
                "generations": removed_generations,
                "cache": [],
                "staging": staging_removed,
            }

        referenced_layers: dict[str, set[str]] = {
            layer_name: set() for layer_name in _SWEPT_CACHE_LAYERS
        }
        for manifest in retained_manifests:
            for cache_layer, entry_hashes in self._manifest_layer_refs(manifest).items():
                referenced_layers[cache_layer].update(entry_hashes)
        for cache_layer, entry_hashes in (ledger_layers or {}).items():
            if cache_layer not in _SWEPT_CACHE_LAYERS:
                raise ValueError(f"unknown ledger cache layer {cache_layer!r}")
            for entry_hash in entry_hashes:
                if not _is_hex64(entry_hash):
                    raise ValueError(
                        "ledger layer refs must be 64-char lowercase hex, "
                        f"got {entry_hash!r} for {cache_layer}"
                    )
                referenced_layers[cache_layer].add(entry_hash)

        cache_root = self._state_root / "cache"
        layer_entries: dict[str, list[tuple[Path, float]]] = {}
        newest_per_layer: dict[str, str] = {}
        for layer_name in _SWEPT_CACHE_LAYERS:
            layer_root = cache_root / layer_name
            if not layer_root.is_dir() or layer_root.is_symlink():
                continue
            entries_for_layer: list[tuple[Path, float]] = []
            for entry in sorted(layer_root.iterdir()):
                if entry.is_symlink() or not entry.is_dir():
                    continue
                if not _is_hex64(entry.name):
                    continue
                try:
                    entries_for_layer.append((entry, now - entry.stat().st_mtime))
                except OSError:
                    continue
            entries_for_layer.sort(key=lambda pair: (pair[1], pair[0].name))
            if entries_for_layer:
                newest_per_layer[layer_name] = entries_for_layer[0][0].name
            layer_entries[layer_name] = entries_for_layer

        removed_cache: list[str] = []
        for layer_name, entries_for_layer in layer_entries.items():
            keep_hashes = referenced_layers[layer_name]
            newest_hash = newest_per_layer.get(layer_name)
            for entry, age in entries_for_layer:
                if entry.name in keep_hashes or entry.name == newest_hash:
                    continue
                if age < grace_seconds:
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                removed_cache.append(f"{layer_name}/{entry.name}")
        return {
            "generations": removed_generations,
            "cache": removed_cache,
            "staging": staging_removed,
        }

    @staticmethod
    def _manifest_layer_refs(manifest: Mapping[str, Any]) -> dict[str, set[str]]:
        """Map one manifest's layer/input refs to cache directory names."""
        refs: dict[str, set[str]] = {layer_name: set() for layer_name in _SWEPT_CACHE_LAYERS}
        layers = manifest.get("layers", {})
        intent = manifest.get("intent", {})
        for manifest_layer, cache_layer in _MANIFEST_LAYER_TO_CACHE_LAYER.items():
            layer = layers.get(manifest_layer) if isinstance(layers, Mapping) else None
            if isinstance(layer, Mapping) and _is_hex64(layer.get("entryHash")):
                refs[cache_layer].add(str(layer["entryHash"]))
            intent_hash = intent.get(manifest_layer) if isinstance(intent, Mapping) else None
            if _is_hex64(intent_hash):
                refs[cache_layer].add(str(intent_hash))
        skin = manifest.get("skin", {})
        skin_hash = skin.get("entryHash") if isinstance(skin, Mapping) else None
        palette = layers.get("palette") if isinstance(layers, Mapping) else None
        palette_hash = palette.get("entryHash") if isinstance(palette, Mapping) else None
        mode = manifest.get("mode")
        host = manifest.get("host")
        host_hash = (
            host.get("fingerprint")
            if isinstance(host, Mapping) and _is_hex64(host.get("fingerprint"))
            else None
        )
        if _is_hex64(palette_hash) and _is_hex64(skin_hash) and mode in ("dark", "light"):
            refs["appearance"].add(
                appearance_generation_hash(
                    str(palette_hash),
                    str(skin_hash),
                    str(mode),
                    host_hash=host_hash,
                )
            )
        return refs
