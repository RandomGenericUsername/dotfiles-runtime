"""Concrete seeding adapter — filesystem I/O for first-run bootstrap.

Implements AD-1 (hexagonal), AD-6 (swap symlinks lead), AD-8 (SHA-256),
AD-9 (staging-dir), AD-11 (first-run self-seeding), AD-14 (domain purity),
AD-16 (hardlink), AD-17 (consumer wiring).

Handles:
- Hardlinking wallpaper into cache (AD-16)
- Atomic symlink repoint (AD-6): tmp symlink + os.replace
- Writing meta.json with hash_algorithm: "sha256" per shared-data-contract
- Creating current/ directory structure

Domain purity (AD-1, AD-14):
- This module lives in ``adapters/`` only (allowed ``os``/``pathlib``/``json``/``uuid``).
- ``domain/`` stays pure — no ``os`` imported there.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from runtime.adapters.cache import hardlink_or_copy
from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec
from runtime.adapters.hashing import HASH_ALGORITHM, hash_file
from runtime.domain.models import (
    EffectsArtifacts,
    EffectsEntry,
    IconsArtifacts,
    IconsEntry,
    PaletteArtifacts,
    PaletteEntry,
)
from runtime.ports.consumer_path_spec import IConsumerPathSpec

logger = logging.getLogger(__name__)

# O_APPEND + O_NOFOLLOW for atomic history append (AD-4).
# O_NOFOLLOW hardens against a symlinked history.jsonl (consistency with the
# current.json symlink guard): appending through a symlink could write
# outside state_root, breaking the AD-5 boundary.
_O_APPEND = os.O_APPEND | os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW

if HASH_ALGORITHM != "sha256":  # pragma: no cover
    raise AssertionError(f"HASH_ALGORITHM must be 'sha256', got {HASH_ALGORITHM!r}")

# Hash algorithm literal for meta.json (AD-8)
_META_HASH_ALGORITHM: Final[str] = "sha256"


def _repoint_symlink(current_path: Path, target: Path) -> None:
    """Atomic symlink repoint: create tmp symlink, os.replace.

    AD-6: symlink repoint is atomic on same filesystem.
    tmp name includes PID + random to avoid collisions.
    The tmp symlink is removed in a finally block so a failed replace
    (e.g. target path exists as a real directory) never leaks
    ``*.tmp.*`` orphans into current/.
    """
    current_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f"{current_path.name}.tmp.{os.getpid()}-{uuid.uuid4().hex[:8]}"
    tmp = current_path.parent / tmp_name
    tmp.symlink_to(target)
    try:
        os.replace(str(tmp), str(current_path))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _now_iso_z() -> str:
    """Current UTC time as strict ISO-8601 ending with Z."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_meta_json(meta_path: Path, data: dict[str, Any]) -> None:
    """Write meta.json atomically (sibling tmp + os.replace)."""
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f"meta.json.tmp.{os.getpid()}-{uuid.uuid4().hex[:8]}"
    tmp = meta_path.parent / tmp_name
    try:
        content = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(meta_path))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class CacheSeeder:
    """Concrete adapter for first-run seeding filesystem I/O.

    Responsibilities:
    - Copy install_spine wallpaper into cache via hardlink (AD-16)
    - Repoint current/ symlinks atomically (AD-6)
    - Write meta.json with hash_algorithm literal per shared-data-contract
    - Ensure current/ directory structure exists
    """

    def __init__(self, state_root: Path, consumer_spec: IConsumerPathSpec | None = None) -> None:
        self._state_root = state_root
        self._consumer_spec: IConsumerPathSpec = consumer_spec or StaticConsumerPathSpec()

    def hardlink_wallpaper(self, src: Path, wallpaper_hash: str) -> Path:
        """Hardlink wallpaper from provisioning into cache (AD-16).

        Delegates to :meth:`import_wallpaper` with ``source_mutable=False``
        (provisioning output is immutable; hardlink is the AD-16 policy).
        """
        return self.import_wallpaper(src, wallpaper_hash, source_mutable=False)

    def import_wallpaper(self, src: Path, wallpaper_hash: str, *, source_mutable: bool) -> Path:
        """Import a wallpaper file into the hash-addressed wallpaper cache.

        One idempotent operation owning the whole wallpaper cache layer:

        - Pre-existing entry: content is verified against ``wallpaper_hash``
          (a matching entry is reused, a mismatching one raises — a
          hash-addressed cache must never hold wrong content).
        - Fresh entry: the content is placed per policy — hardlink for
          immutable provisioning output (AD-16), an atomic copy (tmp +
          ``os.replace``) for user-supplied files so the cache owns its
          bytes and never aliases a mutable source file.
        - Post-place verification: freshly placed content is re-hashed and
          must match ``wallpaper_hash`` — a source mutated between hashing
          and placement cannot poison the hash-addressed entry (the just-
          created entry is removed before raising).
        - Write-once meta backfill: ``meta.json`` is written only when
          absent, so a prior crash mid-entry is repaired on the next import
          and concurrent imports never overwrite each other's provenance.

        Args:
            src: source wallpaper file
            wallpaper_hash: SHA-256 hex of the wallpaper content
            source_mutable: ``True`` for user-supplied files (copy policy),
                ``False`` for immutable provisioning output (hardlink policy)

        Returns:
            Path to the cached wallpaper file

        Raises:
            ValueError: if src is not a regular file
            RuntimeError: if the cache entry exists with different content,
                or freshly placed content does not match its hash address
            OSError: on filesystem failure
        """
        if not src.is_file():
            raise ValueError(f"src must be a regular file, got {src!r}")
        entry_dir = self._state_root / "cache" / "wallpapers" / wallpaper_hash
        dst = entry_dir / "wallpaper.png"
        if dst.exists() or dst.is_symlink():
            # Idempotent re-entry after a crashed first run (FileExistsError
            # from os.link would otherwise block re-seeding forever).
            if dst.is_symlink() or not dst.is_file():
                raise RuntimeError(f"cache entry exists but is not a regular file: {dst}")
            if hash_file(dst) != wallpaper_hash:
                raise RuntimeError(f"cache entry content does not match its hash address: {dst}")
            self._backfill_wallpaper_meta(entry_dir, wallpaper_hash, src)
            return dst
        entry_dir.mkdir(parents=True, exist_ok=True)
        if source_mutable:
            self._copy_owning_bytes(src, dst)
        else:
            hardlink_or_copy(src, dst)
        if hash_file(dst) != wallpaper_hash:
            # The source mutated between hashing and placement (TOCTOU) or
            # the copy was corrupted — never leave wrong content under a
            # hash address.
            shutil.rmtree(entry_dir, ignore_errors=True)
            raise RuntimeError(
                f"cached wallpaper content does not match its hash address "
                f"(source mutated during import?): {dst}"
            )
        self._backfill_wallpaper_meta(entry_dir, wallpaper_hash, src)
        return dst

    def _copy_owning_bytes(self, src: Path, dst: Path) -> None:
        """Copy ``src`` → ``dst`` atomically (sibling tmp + ``os.replace``)."""
        tmp_name = f".wallpaper.png.tmp.{os.getpid()}-{uuid.uuid4().hex[:8]}"
        tmp = dst.parent / tmp_name
        try:
            shutil.copyfile(src, tmp)
            os.replace(str(tmp), str(dst))
        finally:
            tmp.unlink(missing_ok=True)

    def _backfill_wallpaper_meta(self, entry_dir: Path, wallpaper_hash: str, src: Path) -> None:
        """Write wallpaper meta.json only when absent (write-once invariant)."""
        if (entry_dir / "meta.json").exists():
            return
        self.write_wallpaper_meta(
            wallpaper_hash=wallpaper_hash,
            source_path=str(src),
        )

    def write_wallpaper_meta(
        self,
        wallpaper_hash: str,
        source_path: str,
        imported_at: str | None = None,
    ) -> None:
        """Write wallpaper cache meta.json to its final cache entry dir."""
        entry_dir = self._state_root / "cache" / "wallpapers" / wallpaper_hash
        self.write_wallpaper_meta_in(
            entry_dir,
            wallpaper_hash=wallpaper_hash,
            source_path=source_path,
            imported_at=imported_at,
        )

    def write_wallpaper_meta_in(
        self,
        entry_dir: Path,
        *,
        wallpaper_hash: str,
        source_path: str,
        imported_at: str | None = None,
    ) -> None:
        """Write wallpaper cache meta.json into ``entry_dir`` (staging or final).

        Schema per shared-data-contract:
        {hash_algorithm, kind: "wallpaper", content_hash, source_path, imported_at}
        """
        if imported_at is None:
            imported_at = _now_iso_z()
        _write_meta_json(
            entry_dir / "meta.json",
            {
                "hash_algorithm": _META_HASH_ALGORITHM,
                "kind": "wallpaper",
                "content_hash": wallpaper_hash,
                "source_path": source_path,
                "imported_at": imported_at,
            },
        )

    def write_palette_meta(
        self,
        entry_hash: str,
        source_wallpaper_hash: str,
        input_template_hash: str,
        artifact_hashes: dict[str, str],
        generated_at: str | None = None,
    ) -> None:
        """Write palette cache meta.json to its final cache entry dir."""
        entry_dir = self._state_root / "cache" / "palettes" / entry_hash
        self.write_palette_meta_in(
            entry_dir,
            entry_hash=entry_hash,
            source_wallpaper_hash=source_wallpaper_hash,
            input_template_hash=input_template_hash,
            artifact_hashes=artifact_hashes,
            generated_at=generated_at,
        )

    def write_palette_meta_in(
        self,
        entry_dir: Path,
        *,
        entry_hash: str,
        source_wallpaper_hash: str,
        input_template_hash: str,
        artifact_hashes: dict[str, str],
        generated_at: str | None = None,
    ) -> None:
        """Write palette cache meta.json into ``entry_dir`` (staging or final).

        Schema per shared-data-contract:
        {hash_algorithm, kind: "palette", entry_hash, source_wallpaper_hash,
         input_template_hash, artifact_hashes, generated_at}
        """
        if generated_at is None:
            generated_at = _now_iso_z()
        _write_meta_json(
            entry_dir / "meta.json",
            {
                "hash_algorithm": _META_HASH_ALGORITHM,
                "kind": "palette",
                "entry_hash": entry_hash,
                "source_wallpaper_hash": source_wallpaper_hash,
                "input_template_hash": input_template_hash,
                "artifact_hashes": artifact_hashes,
                "generated_at": generated_at,
            },
        )

    def write_effects_meta(
        self,
        entry_hash: str,
        source_wallpaper_hash: str,
        input_catalog_hash: str,
        artifact_hashes: dict[str, str],
        generated_at: str | None = None,
    ) -> None:
        """Write effects cache meta.json to its final cache entry dir."""
        entry_dir = self._state_root / "cache" / "effects" / entry_hash
        self.write_effects_meta_in(
            entry_dir,
            entry_hash=entry_hash,
            source_wallpaper_hash=source_wallpaper_hash,
            input_catalog_hash=input_catalog_hash,
            artifact_hashes=artifact_hashes,
            generated_at=generated_at,
        )

    def write_effects_meta_in(
        self,
        entry_dir: Path,
        *,
        entry_hash: str,
        source_wallpaper_hash: str,
        input_catalog_hash: str,
        artifact_hashes: dict[str, str],
        generated_at: str | None = None,
    ) -> None:
        """Write effects cache meta.json into ``entry_dir`` (staging or final).

        Schema per shared-data-contract:
        {hash_algorithm, kind: "effects", entry_hash, source_wallpaper_hash,
         input_catalog_hash, artifact_hashes, generated_at}
        """
        if generated_at is None:
            generated_at = _now_iso_z()
        _write_meta_json(
            entry_dir / "meta.json",
            {
                "hash_algorithm": _META_HASH_ALGORITHM,
                "kind": "effects",
                "entry_hash": entry_hash,
                "source_wallpaper_hash": source_wallpaper_hash,
                "input_catalog_hash": input_catalog_hash,
                "artifact_hashes": artifact_hashes,
                "generated_at": generated_at,
            },
        )

    def write_icons_meta(
        self,
        entry_hash: str,
        source_palette_hash: str,
        input_templates_hash: str,
        input_mappings_hash: str,
        artifact_hashes: dict[str, str],
        generated_at: str | None = None,
    ) -> None:
        """Write icons cache meta.json to its final cache entry dir."""
        entry_dir = self._state_root / "cache" / "icons" / entry_hash
        self.write_icons_meta_in(
            entry_dir,
            entry_hash=entry_hash,
            source_palette_hash=source_palette_hash,
            input_templates_hash=input_templates_hash,
            input_mappings_hash=input_mappings_hash,
            artifact_hashes=artifact_hashes,
            generated_at=generated_at,
        )

    def write_icons_meta_in(
        self,
        entry_dir: Path,
        *,
        entry_hash: str,
        source_palette_hash: str,
        input_templates_hash: str,
        input_mappings_hash: str,
        artifact_hashes: dict[str, str],
        generated_at: str | None = None,
    ) -> None:
        """Write icons cache meta.json into ``entry_dir`` (staging or final).

        Schema per shared-data-contract:
        {hash_algorithm, kind: "icons", entry_hash, source_palette_hash,
         input_templates_hash, input_mappings_hash, artifact_hashes, generated_at}
        """
        if generated_at is None:
            generated_at = _now_iso_z()
        _write_meta_json(
            entry_dir / "meta.json",
            {
                "hash_algorithm": _META_HASH_ALGORITHM,
                "kind": "icons",
                "entry_hash": entry_hash,
                "source_palette_hash": source_palette_hash,
                "input_templates_hash": input_templates_hash,
                "input_mappings_hash": input_mappings_hash,
                "artifact_hashes": artifact_hashes,
                "generated_at": generated_at,
            },
        )

    def read_entry_meta(self, entry_dir: Path) -> dict[str, Any]:
        """Read a cache entry's meta.json into a dict.

        Raises:
            FileNotFoundError: if meta.json is absent
            ValueError: if meta.json is not valid JSON
        """
        meta_path = entry_dir / "meta.json"
        data: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
        return data

    def load_palette_entry(self, entry_dir: Path) -> PaletteEntry:
        """Rebuild a PaletteEntry from a cache entry's meta.json (real hashes).

        Used when the cache entry already exists (crashed prior run) and the
        adapter was therefore never invoked for it — the state written to
        current.json must still carry the entry's real hashes, not sentinels.
        """
        meta = self.read_entry_meta(entry_dir)
        artifact_hashes: dict[str, str] = meta.get("artifact_hashes", {})
        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=meta["entry_hash"],
            source_wallpaper_hash=meta["source_wallpaper_hash"],
            input_template_hash=meta["input_template_hash"],
            artifact_hashes=PaletteArtifacts(
                colors_yaml=artifact_hashes["colors.yaml"],
                colors_conf=artifact_hashes["colors.conf"],
                colors_gtk_css=artifact_hashes["colors.gtk.css"],
                colors_adw_css=artifact_hashes["colors.adw.css"],
                colors_sequences=artifact_hashes["colors.sequences"],
            ),
            generated_at=meta["generated_at"],
        )

    def load_effects_entry(self, entry_dir: Path) -> EffectsEntry:
        """Rebuild an EffectsEntry from a cache entry's meta.json (real hashes)."""
        meta = self.read_entry_meta(entry_dir)
        artifact_hashes: EffectsArtifacts = cast(
            "EffectsArtifacts", meta.get("artifact_hashes", {})
        )
        return EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash=meta["entry_hash"],
            source_wallpaper_hash=meta["source_wallpaper_hash"],
            input_catalog_hash=meta["input_catalog_hash"],
            artifact_hashes=artifact_hashes,
            generated_at=meta["generated_at"],
        )

    def load_icons_entry(self, entry_dir: Path) -> IconsEntry:
        """Rebuild an IconsEntry from a cache entry's meta.json (real hashes)."""
        meta = self.read_entry_meta(entry_dir)
        artifact_hashes: IconsArtifacts = cast("IconsArtifacts", meta.get("artifact_hashes", {}))
        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=meta["entry_hash"],
            source_palette_hash=meta["source_palette_hash"],
            input_templates_hash=meta["input_templates_hash"],
            input_mappings_hash=meta["input_mappings_hash"],
            artifact_hashes=artifact_hashes,
            generated_at=meta["generated_at"],
        )

    def drain_work_dir(self, work_dir: Path, staging_dir: Path) -> None:
        """Move adapter-generated artifacts from work dir into the staging dir.

        Real CSG/WEG/ITR adapters validate that their output dir's *name*
        equals the expected entry hash, so they must be given
        ``staging_dir / <entry_hash>``. After generation, the artifacts are
        drained (structure-preserving) into the staging dir root where
        ``populate_via_staging`` expects them plus ``meta.json``.

        WEG's ``OutputPathService.batch_output_dir`` creates a nested
        ``<wallpaper_stem>/`` subdir under the work dir (e.g.
        ``<work>/<stem>/effect/*.png``). The move loop preserves that
        structure into ``staging_dir``; the cleanup at the end removes
        the work dir AND its now-empty nested subdirs (the previous
        ``work_dir.rmdir()`` failed with ENOTEMPTY when WEG's nested
        structure left intermediate dirs behind).
        """
        for item in sorted(work_dir.rglob("*")):
            dest = staging_dir / item.relative_to(work_dir)
            if item.is_dir() and not item.is_symlink():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.rename(item, dest)
        shutil.rmtree(work_dir, ignore_errors=True)

    def ensure_current_dir(self) -> Path:
        """Ensure current/ directory exists under state_root.

        Returns:
            Path to the current/ directory
        """
        current_dir = self._state_root / "current"
        current_dir.mkdir(parents=True, exist_ok=True)
        return current_dir

    def repoint_current_symlink(self, name: str, target: Path) -> Path:
        """Repoint a single current/ symlink atomically (AD-6).

        Args:
            name: symlink name (e.g., "wallpaper-DP-1.png", "colors.conf")
            target: target path (must exist or be resolvable)

        Returns:
            Path to the created/updated symlink
        """
        current_dir = self.ensure_current_dir()
        symlink_path = current_dir / name
        _repoint_symlink(symlink_path, target)
        return symlink_path

    def repoint_current_symlinks(
        self,
        wallpaper_target: Path,
        monitor_names: list[str],
        palette_entry_hash: str | None = None,
        effects_entry_hash: str | None = None,
        icons_entry_hash: str | None = None,
    ) -> list[Path]:
        """Repoint all current/ symlinks atomically (AD-6, AD-17).

        Creates symlinks in current/ pointing to cache entries:
        - current/wallpaper-<monitor>.png → cache/wallpapers/<wh>/wallpaper.png
        - current/colors.conf → cache/palettes/<ph>/colors.conf
        - current/colors.gtk.css → cache/palettes/<ph>/colors.gtk.css
        - current/colors.yaml → cache/palettes/<ph>/colors.yaml
        - current/colors.adw.css → cache/palettes/<ph>/colors.adw.css
        - current/colors.sequences → cache/palettes/<ph>/colors.sequences
        - current/effects/ → cache/effects/<eh>/
        - current/icons/ → cache/icons/<ih>/

        Returns:
            List of created/updated symlink paths
        """
        created: list[Path] = []

        # Wallpaper symlinks per monitor
        for monitor_name in monitor_names:
            name = f"wallpaper-{monitor_name}.png"
            created.append(self.repoint_current_symlink(name, wallpaper_target))

        # Monitor-agnostic wallpaper alias (gt-4-2 follow-up): consumers that
        # don't know monitor names (wlogout background) read this one.
        if monitor_names:
            created.append(self.repoint_current_symlink("wallpaper.png", wallpaper_target))

        # Palette symlinks
        if palette_entry_hash is not None:
            palette_dir = self._state_root / "cache" / "palettes" / palette_entry_hash
            for artifact_name in (
                "colors.conf",
                "colors.gtk.css",
                "colors.yaml",
                "colors.adw.css",
                "colors.sequences",
            ):
                target = palette_dir / artifact_name
                # exists() follows symlinks — a dangling symlink at the target
                # path is still repointed so it never lingers half-broken.
                if target.exists() or target.is_symlink():
                    created.append(self.repoint_current_symlink(artifact_name, target))
                else:
                    logger.warning(
                        "seeding: palette artifact missing, consumer symlink skipped: %s",
                        target,
                    )

        # Effects directory symlink
        if effects_entry_hash is not None:
            effects_dir = self._state_root / "cache" / "effects" / effects_entry_hash
            if effects_dir.exists() or effects_dir.is_symlink():
                created.append(self.repoint_current_symlink("effects", effects_dir))
            else:
                logger.warning("seeding: effects entry missing, consumer symlink skipped")

        # Icons directory symlink
        if icons_entry_hash is not None:
            icons_dir = self._state_root / "cache" / "icons" / icons_entry_hash
            if icons_dir.exists() or icons_dir.is_symlink():
                created.append(self.repoint_current_symlink("icons", icons_dir))
            else:
                logger.warning("seeding: icons entry missing, consumer symlink skipped")

        return created

    def repoint_consumer_symlinks(
        self,
        install_spine: Path,
        palette_entry_hash: str | None,
    ) -> list[Path]:
        """Repoint spine consumer pointers at ``current/`` (R2, Epic 4).

        AD-11 exception (recorded in ARCHITECTURE-SPINE.md): the seeder
        may write under the install spine ONLY the pointer symlinks
        returned by the injected :class:`IConsumerPathSpec` (the pinned
        ``StaticConsumerPathSpec`` table: ``config/ags/colors.css``,
        ``config/gtk-3.0/colors.css``, ``config/gtk-4.0/colors.css``) —
        nothing else. The content lives in ``cache/`` (runtime-owned);
        these paths are consumer pointers.

        - ``<install>/config/ags/colors.css`` → ``current/colors.gtk.css``
          (note the rename: cache artifact is ``colors.gtk.css``).
        - ``<install>/config/gtk-3.0/colors.css`` →
          ``current/colors.gtk.css``.
        - ``<install>/config/gtk-4.0/colors.css`` →
          ``current/colors.adw.css``.

        Deliberately NOT covered: ``<install>/config/hypr/colors.conf``
        (nothing sources it — verified: no reference under
        ``dotfiles/config/hypr/*.lua``) and hyprpaper.conf / ITR
        ``color_scheme.path`` rewrites (dissolved: hyprpaper uses IPC
        with the resolved ``current/`` path; the ITR settings template
        points at ``current/colors.yaml`` as a static string).

        Args:
            install_spine: provisioning install spine root (read for
                validation, written ONLY for the spec'd pointer symlinks).
            palette_entry_hash: palette entry hash, or ``None`` when the
                palette layer is null.

        Returns:
            List of created/updated symlink paths (empty when skipped).

        Behavior (rules read ONCE from the spec, semantics implemented
        exactly once here — investigation §3):
        - ``None`` palette: every existing pointer dest is REMOVED
          (``missing_ok`` semantics) so consumers never serve a stale
          palette as if current; AGS falls back to its bundled default.
        - A regular FILE at the destination (pre-Epic-4 provisioning
          copy on upgraded machines) is REPLACED with the symlink —
          one ``wallpaper set`` migrates (``_repoint_symlink``'s tmp +
          ``os.replace`` is atomic; never delete-then-create).
        - A missing ``current/<target>`` artifact skips that pointer
          with a warning — never a dangling consumer symlink.
        - A missing destination PARENT dir skips that pointer with a
          warning — never ``mkdir`` under the install spine (the spine
          dirs are provisioning's: ``config/gtk-{3,4}.0/`` arrive in
          Story gt-3-1).
        """
        pointers = self._consumer_spec.consumer_pointers()
        rules = self._consumer_spec.rules()
        dests = [install_spine / pointer.path for pointer in pointers]
        if palette_entry_hash is None:
            if rules.remove_on_null_palette:
                for dest in dests:
                    try:
                        if dest.is_symlink() or dest.exists():
                            dest.unlink()
                            logger.warning(
                                "seeding: palette layer is null; consumer symlink removed: %s",
                                dest,
                            )
                    except OSError as exc:
                        logger.warning("seeding: cannot remove consumer symlink %s: %s", dest, exc)
            return []
        created: list[Path] = []
        for pointer, dest in zip(pointers, dests, strict=True):
            target = self._state_root / "current" / pointer.target
            if rules.skip_on_missing_target and not target.exists() and not target.is_symlink():
                logger.warning(
                    "seeding: current/%s missing, consumer symlink skipped",
                    pointer.target,
                )
                continue
            if rules.skip_on_missing_parent and not dest.parent.is_dir():
                logger.warning(
                    "seeding: consumer symlink skipped, destination parent missing: %s",
                    dest,
                )
                continue
            _repoint_symlink(dest, target)
            created.append(dest)
        return created

    def append_history(
        self,
        trigger: str,
        wallpaper_hash: str,
        palette_hash: str | None = None,
        effects_hash: str | None = None,
        icons_hash: str | None = None,
        source_path: str = "",
    ) -> None:
        """Append a line to history.jsonl atomically (AD-4, AR-3).

        Uses O_APPEND + os.fsync for atomic persistence guarantee.
        Never truncates or rewrites — append only.

        Design decision (Story rt-3.1): history stays on this CacheSeeder
        adapter, NOT promoted to ``IStateRepository`` — rt-1-10 pinned the
        port minimal (``load_current``/``save`` only); Epic 3 inspection use
        cases consume history through the adapter seam. This is the ONLY
        history writer; call it through Reconcile/Seed, never from the CLI
        (rt-2-7 invariant).

        Args:
            trigger: event trigger — the pinned enum is
                ``seed|set|reconcile|force`` ("apply" is NOT valid); this
                adapter stays trigger-agnostic and does not validate.
            wallpaper_hash: SHA-256 hex of wallpaper content
            palette_hash: SHA-256 hex of palette entry or None
            effects_hash: SHA-256 hex of effects entry or None
            icons_hash: SHA-256 hex of icons entry or None
            source_path: source path or empty string
        """
        history_path = self._state_root / "history.jsonl"
        line = (
            json.dumps(
                {
                    "ts": _now_iso_z(),
                    "trigger": trigger,
                    "wallpaper": wallpaper_hash,
                    "palette": palette_hash,
                    "effects": effects_hash,
                    "icons": icons_hash,
                    "source_path": source_path,
                },
                ensure_ascii=False,
            )
            + "\n"
        )

        fd = os.open(str(history_path), _O_APPEND, 0o644)
        try:
            # Loop until the whole line is written — os.write may perform a
            # short write under memory pressure, and a partial line would
            # corrupt the append-only JSONL log (AD-4).
            data = memoryview(line.encode("utf-8"))
            while data:
                written = os.write(fd, data)
                data = data[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
