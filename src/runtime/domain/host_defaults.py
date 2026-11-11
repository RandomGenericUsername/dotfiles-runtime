"""Pure host-default resolution for the v2 appearance plane.

A skin may delegate an explicit allowlist of logical token paths to a live
host. The host snapshot is resolved after mode selection and before the
resolved token document is validated/rendered:

    mode override > host value > skin fallback

Only the values that actually participated enter ``hostHash``; an unavailable
host therefore produces the same identity as a host whose delegated values
were never used. This module is pure Python (no filesystem or subprocess I/O).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

HOST_SOURCE = "hyprland"
HOST_MAP_VERSION = 1

#: Phase 1 mapping: logical token path -> Hyprland effective option.
#:
#: The outer window/toplevel and panel geometry and the inner card/control/cell
#: rectangle radii follow the one compositor rounding value.
#: ``surface.pill.radius`` is deliberately absent: a pill is a capsule and its
#: identity must stay explicit. ``surface.window.padding`` and
#: ``surface.window.opacity`` are absent too: the host has no honest toplevel
#: padding or opacity source. Border width is delegable only for the window and
#: the panel.
HOST_MAP: Mapping[str, str] = {
    "surface.window.radius": "decoration:rounding",
    "surface.window.border-width": "general:border_size",
    "surface.panel.radius": "decoration:rounding",
    "surface.panel.border-width": "general:border_size",
    "surface.card.radius": "decoration:rounding",
    "surface.control.radius": "decoration:rounding",
    "surface.cell.radius": "decoration:rounding",
}

#: Valid ranges for the seven delegated numeric tokens.
HOST_BOUNDS: Mapping[str, float] = {
    "surface.window.radius": 64.0,
    "surface.window.border-width": 16.0,
    "surface.panel.radius": 64.0,
    "surface.panel.border-width": 16.0,
    "surface.card.radius": 64.0,
    "surface.control.radius": 64.0,
    "surface.cell.radius": 64.0,
}

HOST_FALLBACKS = frozenset({"skin", "error"})


class HostDefaultsError(ValueError):
    """A delegated host value is missing/invalid and fallback is ``error``."""


def validate_host_value(path: str, value: object) -> int | float:
    """Return a canonical finite non-negative host value in the token range."""
    if path not in HOST_BOUNDS:
        raise ValueError(f"unknown host token path {path!r}")
    if isinstance(value, bool):
        raise ValueError(f"{path}: bool is not a numeric host value ({value!r})")
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"{path}: expected a number, got {type(value).__name__} ({value!r})"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{path}: non-finite host value ({value!r})")
    if number < 0:
        raise ValueError(f"{path}: negative host value ({value!r})")
    maximum = HOST_BOUNDS[path]
    if number > maximum:
        raise ValueError(f"{path}: host value {value!r} exceeds bound {maximum:g}")
    return int(value) if isinstance(value, int) or number.is_integer() else number


@dataclass(frozen=True, slots=True)
class HostSnapshot:
    """One read of the live host; only delegated values are retained."""

    values: Mapping[str, int | float] = field(default_factory=dict)
    source: str = HOST_SOURCE
    version: int = HOST_MAP_VERSION
    instance_signature: str | None = None
    warnings: tuple[str, ...] = ()

    def fingerprint(self, paths: Iterable[str] | None = None) -> str | None:
        """Canonical hash over the mapping version and participating values.

        ``paths`` selects the values that actually participated in the selected
        skin/mode. Instance signatures, timestamps and option ``set`` flags are
        deliberately excluded: a compositor restart with the same effective
        values is the same appearance.
        """
        selected = sorted(set(self.values) if paths is None else set(paths))
        selected = [path for path in selected if path in self.values]
        if not selected:
            return None
        payload = {
            "version": self.version,
            "values": {path: self.values[path] for path in selected},
        }
        material = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HostResolution:
    """Resolved tokens plus the host provenance that produced them."""

    tokens: dict[str, Any]
    provenance: Mapping[str, str]
    host_hash: str | None
    defaults: Mapping[str, int | float]
    warnings: tuple[str, ...] = ()

    @property
    def host_used(self) -> bool:
        return any(source == "host" for source in self.provenance.values())


def _set_token(tokens: dict[str, Any], path: str, value: int | float) -> None:
    parts = path.split(".")
    cursor: Any = tokens
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            raise ValueError(f"host token path {path!r} is not in the token document")
        cursor = cursor[part]
    if not isinstance(cursor, dict):
        raise ValueError(f"host token path {path!r} is not an object token")
    cursor[parts[-1]] = value


def resolve_host_defaults(
    tokens: Mapping[str, Any],
    manifest: Mapping[str, Any],
    mode: str | None,
    snapshot: HostSnapshot | None,
) -> HostResolution:
    """Apply one skin's ``[host]`` policy to a mode-resolved token document.

    A complete mode group is explicit and wins over the host. A token not in
    ``inherit`` is never touched. Missing/invalid host values use the skin's
    selected concrete value, or fail closed when ``fallback = "error"``.
    """
    resolved = copy.deepcopy(dict(tokens))
    policy = manifest.get("host")
    if policy is None:
        return HostResolution(
            tokens=resolved,
            provenance={},
            host_hash=None,
            defaults={},
        )
    if not isinstance(policy, Mapping):
        raise ValueError("appearance manifest host policy must be an object")
    inherit = policy.get("inherit")
    if not isinstance(inherit, list) or not inherit or not all(
        isinstance(path, str) and path in HOST_MAP for path in inherit
    ):
        raise ValueError("appearance manifest host.inherit must be a non-empty known-path list")
    if len(set(inherit)) != len(inherit):
        raise ValueError("appearance manifest host.inherit must be unique")
    fallback = policy.get("fallback", "skin")
    if fallback not in HOST_FALLBACKS:
        raise ValueError(f"appearance manifest host.fallback must be skin|error, got {fallback!r}")

    overrides = manifest.get("mode")
    selected_mode = (
        overrides.get(mode) if isinstance(overrides, Mapping) and mode is not None else None
    )
    explicit_groups = set(selected_mode) if isinstance(selected_mode, Mapping) else set()

    provenance: dict[str, str] = {}
    defaults: dict[str, int | float] = {}
    warnings: list[str] = list(snapshot.warnings) if snapshot is not None else []
    for path in inherit:
        group = path.split(".", 1)[0]
        if group in explicit_groups:
            provenance[path] = "mode-override"
            continue
        raw = snapshot.values.get(path) if snapshot is not None else None
        try:
            value = validate_host_value(path, raw) if raw is not None else None
        except ValueError as exc:
            warnings.append(str(exc))
            value = None
        if value is None:
            if fallback == "error":
                raise HostDefaultsError(
                    f"host value unavailable for inherited token {path!r}"
                    + (f": {warnings[-1]}" if warnings else "")
                )
            provenance[path] = "skin-fallback"
            continue
        _set_token(resolved, path, value)
        provenance[path] = "host"
        defaults[path] = value

    host_hash = snapshot.fingerprint(defaults) if snapshot is not None and defaults else None
    return HostResolution(
        tokens=resolved,
        provenance=provenance,
        host_hash=host_hash,
        defaults=defaults,
        warnings=tuple(warnings),
    )
