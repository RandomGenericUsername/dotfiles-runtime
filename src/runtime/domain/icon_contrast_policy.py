"""Per-wallpaper icon-contrast preference policy (pure domain).

Owns the resolution precedence for the icon-contrast guard opt-out
(openspec ``icon-contrast-opt-out``): explicit CLI flag beats the
per-wallpaper store, which beats the default-ON fallback.

Purity (AD-1, AD-14): this module is TOTAL pure — no ``pathlib``, no file
I/O, no ``json`` import (the layering gate allowlists only
``__future__``/``dataclasses``/``enum``/``typing``/``collections``/
``functools``/``re`` in ``domain/``). The store file is therefore parsed
and serialized with a documented regex-subset reader over the exact shape
the adapter writes (``{"version": 1, "prefs": {<64-hex>: bool}}``);
anything outside that shape raises ``ValueError`` and the adapter maps
it to empty prefs + warning (never into the pipeline).
"""

from __future__ import annotations

import re
from typing import Final

#: Store schema version. A file carrying any other ``version`` is rejected
#: by :func:`parse` (the adapter then treats it as corrupt ⇒ empty prefs).
PREFS_VERSION: Final[int] = 1

#: Accepted CLI flag values for ``--contrast`` (``wallpaper set`` and
#: ``icons regenerate`` share them).
CONTRAST_FLAGS: Final[tuple[str, str, str]] = ("auto", "on", "off")

#: Policy provenance recorded in ``meta.json: contrast.policy.source``.
POLICY_SOURCES: Final[tuple[str, str, str]] = ("flag", "store", "default")

_HASH_RE: Final[str] = r"[0-9a-fA-F]{64}"
_VERSION_RE: Final[re.Pattern[str]] = re.compile(r'"version"\s*:\s*(-?\d+)')
_PREFS_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r'"prefs"\s*:\s*\{(?P<body>[^}]*)\}', re.DOTALL
)
_ENTRY_RE: Final[re.Pattern[str]] = re.compile(r'"(?P<key>[^"]+)"\s*:\s*(?P<value>true|false)')
_FULL_HASH_RE: Final[re.Pattern[str]] = re.compile(rf"^{_HASH_RE}$")


def is_wallpaper_hash(value: object) -> bool:
    """True when ``value`` is a 64-hex wallpaper content hash (a valid pref key)."""
    return isinstance(value, str) and _FULL_HASH_RE.match(value) is not None


def parse(text: str) -> dict[str, bool]:
    """Parse store file text into ``{wallpaper_hash: enabled}`` prefs.

    Accepts exactly the adapter-written shape (key order and insignificant
    whitespace are free). Raises ``ValueError`` on anything else —
    non-object text, a missing/non-object ``prefs`` block, a
    ``version`` other than :data:`PREFS_VERSION`, non-64-hex keys, or
    non-boolean values — so the adapter can degrade to empty prefs with
    a warning. An absent ``version`` is treated as ``1`` (forward-tolerant
    read of files written before versioning, if any ever existed).
    """
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        raise ValueError("icon-contrast prefs must be a JSON object")
    version_match = _VERSION_RE.search(stripped)
    if version_match is not None and int(version_match.group(1)) != PREFS_VERSION:
        raise ValueError(f"unsupported icon-contrast prefs version: {version_match.group(1)}")
    block_match = _PREFS_BLOCK_RE.search(stripped)
    if block_match is None:
        raise ValueError("icon-contrast prefs object has no 'prefs' mapping")
    body = block_match.group("body")
    prefs: dict[str, bool] = {}
    for entry in _ENTRY_RE.finditer(body):
        key = entry.group("key")
        if not is_wallpaper_hash(key):
            raise ValueError(f"invalid icon-contrast prefs key: {key!r}")
        prefs[key.lower()] = entry.group("value") == "true"
    remainder = _ENTRY_RE.sub("", body)
    if remainder.strip().strip(",").strip():
        raise ValueError("malformed entries in icon-contrast prefs mapping")
    return prefs


def serialize(prefs: dict[str, bool]) -> str:
    """Serialize prefs to canonical store text (sorted keys, trailing newline).

    Raises ``ValueError`` on non-64-hex keys or non-bool values — the
    store must never persist a shape :func:`parse` would reject.
    """
    for key, value in prefs.items():
        if not is_wallpaper_hash(key):
            raise ValueError(f"invalid icon-contrast prefs key: {key!r}")
        if not isinstance(value, bool):
            raise ValueError(f"invalid icon-contrast prefs value for {key!r}: {value!r}")
    entries = ", ".join(
        f'"{key.lower()}": {"true" if prefs[key] else "false"}' for key in sorted(prefs)
    )
    return '{"version": 1, "prefs": {' + entries + "}}\n"


def lookup(prefs: dict[str, bool], wallpaper_hash: str) -> bool | None:
    """Return the stored choice for ``wallpaper_hash``, or ``None`` when absent.

    Absent (``None``) means default-ON downstream — the just-shipped guard
    stays active everywhere unless explicitly opted out.
    """
    return prefs.get(wallpaper_hash)


def resolve(
    *,
    flag: str,
    stored: bool | None,
    default: bool = True,
) -> tuple[bool, str]:
    """Resolve ``(enabled, source)`` per the precedence table.

    Highest first: explicit CLI flag (``on``/``off`` → ``"flag"``) >
    per-wallpaper store (``"store"``) > default (``"default"``, ON unless
    the caller passes ``default=False``). Raises ``ValueError`` on an
    unknown flag — fail loud, never guess.
    """
    if flag == "on":
        return True, "flag"
    if flag == "off":
        return False, "flag"
    if flag == "auto":
        if stored is None:
            return default, "default"
        return stored, "store"
    raise ValueError(
        f"invalid contrast flag: {flag!r} (expected one of {', '.join(CONTRAST_FLAGS)})"
    )


def describe(*, enabled: bool, source: str) -> str:
    """Human one-liner explaining which policy rendered an icons entry.

    Used by ``inspect``/``doctor`` so the stored ``contrast.policy`` is
    traceable without reading ``meta.json`` by hand.
    """
    state = "ON" if enabled else "OFF"
    if source == "store":
        if enabled:
            return "icons rendered with guard ON (per-wallpaper preference)"
        return "icons rendered with guard OFF via per-wallpaper preference"
    if source == "flag":
        return f"icons rendered with guard {state} (explicit --contrast flag)"
    if source == "default":
        return f"icons rendered with guard {state} (default)"
    raise ValueError(
        f"invalid policy source: {source!r} (expected one of {', '.join(POLICY_SOURCES)})"
    )
