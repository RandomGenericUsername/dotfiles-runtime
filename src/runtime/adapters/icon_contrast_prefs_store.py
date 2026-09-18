"""Per-wallpaper icon-contrast preference store (adapter — file I/O lives here).

The store is machine-local state at ``$XDG_STATE_HOME/dotfiles/
icon-contrast.json`` (in practice ``<state_root>/icon-contrast.json`` —
``state_root`` already resolves the XDG base, session-scoped or not):
``{"version": 1, "prefs": {<64-hex wallpaper hash>: bool}}``. Absent file
or absent key resolves to enabled (default ON). The GUI never writes
this file directly — it shells ``icons preference`` (the sole interface).

Hexagonal split (D1a): the pure shape (parse/serialize/lookup/resolve)
lives in ``runtime.domain.icon_contrast_policy``; this module owns ONLY
reads, atomic temp+rename writes, and the governing-hash lookup. Reads
never raise into the pipeline: a missing file is ``{}`` (all defaults)
and a corrupt file is ``{}`` + warning. Writes validate strictly and let
``OSError`` propagate to the CLI, which warns and continues the run with
the forced value.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Final

from runtime.adapters.hashing import hash_file
from runtime.domain.icon_contrast_policy import (
    is_wallpaper_hash,
    lookup,
    parse,
    resolve,
    serialize,
)

logger = logging.getLogger(__name__)

#: Store filename under ``state_root`` (never provisioned, never in the repo).
STORE_FILENAME: Final[str] = "icon-contrast.json"

_FULL_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def store_path(state_root: Path) -> Path:
    """Return the preference store path for ``state_root``."""
    return state_root / STORE_FILENAME


def read_prefs(store_file: Path) -> dict[str, bool]:
    """Read prefs; missing file ⇒ ``{}``, corrupt file ⇒ ``{}`` + warning.

    Never raises: ``OSError``/``UnicodeDecodeError``/``ValueError`` all
    degrade to empty prefs so a damaged state file can never break icon
    derivation (the guard default-ON applies).
    """
    try:
        text = store_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("icon-contrast prefs unreadable (%s); using defaults: %s", store_file, exc)
        return {}
    except UnicodeDecodeError as exc:
        logger.warning("icon-contrast prefs not UTF-8 (%s); using defaults: %s", store_file, exc)
        return {}
    try:
        return parse(text)
    except ValueError as exc:
        logger.warning("icon-contrast prefs corrupt (%s); using defaults: %s", store_file, exc)
        return {}


def write_pref(store_file: Path, wallpaper_hash: str, enabled: bool) -> dict[str, bool]:
    """Persist one choice atomically (sibling tmp + ``os.replace``).

    Read-modify-write over :func:`read_prefs` (tolerant read), so a
    corrupt file is healed to just this entry. Returns the updated prefs.

    Raises:
        ValueError: on a non-64-hex hash or non-bool value (programmer
            error — the CLI validates user input before calling).
        OSError: when the write itself fails (the CLI warns and continues
            the run with the forced value).
    """
    if not is_wallpaper_hash(wallpaper_hash):
        raise ValueError(f"invalid wallpaper hash for icon-contrast pref: {wallpaper_hash!r}")
    if not isinstance(enabled, bool):
        raise ValueError(f"icon-contrast pref must be bool, got {enabled!r}")
    prefs = read_prefs(store_file)
    prefs[wallpaper_hash.lower()] = enabled
    payload = serialize(prefs)
    store_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f"{STORE_FILENAME}.tmp.{os.getpid()}-{uuid.uuid4().hex[:8]}"
    tmp = store_file.parent / tmp_name
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(store_file))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return prefs


def resolve_contrast(
    *,
    flag: str,
    governing_hash: str,
    store_file: Path,
    default: bool = True,
) -> tuple[bool, str]:
    """Resolve ``(enabled, source)`` for one wallpaper hash (shared helper).

    Both ``wallpaper set`` and ``icons regenerate`` call this one function
    (D2): explicit ``on``/``off`` forces the run (persistence is the
    caller's job, before deriving); ``auto`` reads the store, falling
    back to default-ON. Raises ``ValueError`` on an unknown flag.
    """
    stored = lookup(read_prefs(store_file), governing_hash)
    return resolve(flag=flag, stored=stored, default=default)


def governing_wallpaper_hash(input_path: Path, state_root: Path, *, fallback_hash: str = "") -> str:
    """Resolve the wallpaper hash a contrast choice is stored under (D2a).

    A direct wallpaper input governs itself (its content hash). A WEG
    artifact input (a file under ``<state_root>/cache/effects/``, detected
    with the existing ``_is_weg_artifact``-style path check) has its OWN
    content hash, but the user's choice lives on the parent wallpaper —
    so the effects entry's ``source_wallpaper_hash`` (read-only from the
    entry ``meta.json``) governs. When the parent hash is unresolvable
    (missing/corrupt meta, unreadable input), fall back to the input's
    content hash — or ``fallback_hash`` when the input itself cannot be
    hashed (e.g. ``icons regenerate`` on a deleted source file, where the
    live ``current.json`` hash is authoritative) — with a warning; the
    default-ON policy then applies. Raises ``OSError``/``ValueError``
    only when nothing at all can be hashed and no fallback was given.
    """
    try:
        resolved = input_path.expanduser().resolve()
    except OSError:
        resolved = input_path
    try:
        root = state_root.expanduser().resolve()
    except OSError:
        root = state_root
    parent: str | None = None
    try:
        is_artifact = resolved.is_relative_to(root / "cache" / "effects")
    except OSError, ValueError:
        is_artifact = False
    if is_artifact:
        parent = _read_parent_hash(resolved, root)
        if parent is not None:
            return parent
        logger.warning(
            "icon-contrast: cannot resolve parent wallpaper for variant %s; "
            "using the variant content hash (default ON applies)",
            input_path,
        )
    try:
        return hash_file(resolved)
    except (OSError, ValueError) as exc:
        if fallback_hash:
            logger.warning(
                "icon-contrast: cannot hash %s (%s); using provided hash (default ON applies)",
                input_path,
                exc,
            )
            return fallback_hash
        raise


def _read_parent_hash(variant_path: Path, state_root: Path) -> str | None:
    """Return the effects entry's ``source_wallpaper_hash``, or ``None``.

    ``None`` covers every unresolvable shape: a path that escapes the
    entry layout, a missing/unreadable/corrupt ``meta.json``, or a
    non-64-hex recorded hash — the caller falls back with a warning.
    """
    try:
        rel = variant_path.relative_to(state_root / "cache" / "effects")
    except OSError, ValueError:
        return None
    if not rel.parts:
        return None
    meta_path = state_root / "cache" / "effects" / rel.parts[0] / "meta.json"
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError, ValueError, UnicodeDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    candidate = data.get("source_wallpaper_hash")
    if isinstance(candidate, str) and _FULL_HEX_RE.match(candidate):
        return candidate
    return None


def read_icons_policy(state_root: Path, icons_entry_hash: str) -> dict[str, Any] | None:
    """Return an icons entry's ``contrast.policy`` (``{source, enabled}``).

    Read-only projection for ``inspect``/``doctor`` wording. ``None``
    when the entry is absent, unreadable, corrupt, or predates the
    policy field (old entries stay valid) — callers omit the wording
    instead of guessing. Never raises.
    """
    meta_path = state_root / "cache" / "icons" / icons_entry_hash / "meta.json"
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError, ValueError, UnicodeDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    contrast = data.get("contrast")
    if not isinstance(contrast, dict):
        return None
    policy = contrast.get("policy")
    if not isinstance(policy, dict):
        return None
    enabled = policy.get("enabled")
    source = policy.get("source")
    if not isinstance(enabled, bool) or source not in ("flag", "store", "default"):
        return None
    return {"source": source, "enabled": enabled}
