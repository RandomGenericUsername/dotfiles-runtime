"""Desired-state file reader (Phase 4, Story 4.2).

Reads ``state_root/desired.json`` — the declared intent the future diff
engine converges toward. All filesystem I/O for desired state lives here
(AD-25); the model itself is pure (``domain.models.DesiredState``).

v1 schema contract (validated strictly — fail loud on typos, matching repo
philosophy)::

    {"version": 1, "wallpaper": "<path>", "keep": 5, "pinned": ["<64hex>", ...]}

- Absent file → ``None`` ("no declared intent"; plain ``reconcile`` keeps
  today's imperative behavior). Never creates the file — readers don't
  provision.
- Symlinked file → ``ValueError`` (refuse to follow: a redirectable intent
  file is a spoofing vector; mirrors the history/meta symlink policy).
- Unparseable / non-object / wrong ``version`` / empty ``wallpaper`` /
  negative or non-int ``keep`` / non-list ``pinned`` / non-64-hex pin /
  unknown key → ``ValueError`` naming the file and the offending field.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

from runtime.domain.models import DesiredState

DESIRED_FILENAME = "desired.json"
DESIRED_VERSION = 1

_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_lower_hex64(value: str) -> bool:
    """Strict 64-char lowercase hex (canonical entry-hash form, no case fold)."""
    return len(value) == 64 and all(c in _HEX_DIGITS for c in value)


def read_desired_state(state_root: Path) -> DesiredState | None:
    """Load and validate ``state_root/desired.json``.

    Returns ``None`` when the file is absent. Raises ``ValueError`` (naming
    file + field) on any schema violation. Never writes.
    """
    desired_path = state_root / DESIRED_FILENAME
    if desired_path.is_symlink():
        raise ValueError(f"desired state is a symlink (refusing to follow): {desired_path}")
    try:
        fd = os.open(desired_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP or desired_path.is_symlink():
            raise ValueError(
                f"desired state is a symlink (refusing to follow): {desired_path}"
            ) from exc
        raise ValueError(f"cannot read desired state {desired_path}: {exc}") from exc
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            raw = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read desired state {desired_path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"desired state is not valid JSON: {desired_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"desired state must be a JSON object: {desired_path}")
    unknown = set(data) - {"version", "wallpaper", "keep", "pinned"}
    if unknown:
        raise ValueError(f"desired state has unknown keys {sorted(unknown)}: {desired_path}")
    version = data.get("version", "<absent>")
    if type(version) is not int or version != DESIRED_VERSION:
        raise ValueError(
            f"desired state version must be {DESIRED_VERSION}, got {version!r}: {desired_path}"
        )
    wallpaper = data.get("wallpaper")
    if not isinstance(wallpaper, str) or not wallpaper:
        raise ValueError(f"desired state 'wallpaper' must be a non-empty string: {desired_path}")
    keep = data.get("keep")
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 0:
        raise ValueError(
            f"desired state 'keep' must be an integer >= 0, got {keep!r}: {desired_path}"
        )
    pinned = data.get("pinned")
    if not isinstance(pinned, list):
        raise ValueError(f"desired state 'pinned' must be a list of 64-char hashes: {desired_path}")
    for entry_hash in pinned:
        if not isinstance(entry_hash, str) or not _is_lower_hex64(entry_hash):
            raise ValueError(
                f"desired state pinned entry must be 64-char lowercase hex, "
                f"got {entry_hash!r}: {desired_path}"
            )
    return DesiredState(wallpaper=wallpaper, keep=keep, pinned=tuple(pinned))
