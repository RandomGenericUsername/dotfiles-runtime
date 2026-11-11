"""Hyprland host-defaults adapter (read-only, no polling).

Effective values come from ``hyprctl -j getoption``, never from config text:
later Lua settings can override earlier declarations. The adapter prefers
``HYPRLAND_INSTANCE_SIGNATURE`` and, when it is absent, accepts exactly one
instance reported by ``hyprctl -j instances``; zero or multiple instances are
unavailable rather than guessed.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from runtime.adapters.hyprland_reloader import _resolve_hyprctl
from runtime.domain.host_defaults import (
    HOST_MAP,
    HOST_MAP_VERSION,
    HOST_SOURCE,
    HostSnapshot,
    validate_host_value,
)
from runtime.ports.host_defaults import IHostDefaults

logger = logging.getLogger(__name__)

_SUBPROCESS_ERRORS: tuple[type[BaseException], ...] = (
    FileNotFoundError,
    PermissionError,
    subprocess.TimeoutExpired,
    OSError,
    ValueError,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


class HyprlandHostDefaults(IHostDefaults):
    """Read the five Phase 1 delegated values from the live compositor."""

    def __init__(
        self,
        hyprctl_path: Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = 10,
        runner: Runner | None = None,
    ) -> None:
        self._hyprctl_path = _resolve_hyprctl(hyprctl_path)
        self._env = dict(env) if env is not None else None
        self._timeout = timeout
        self._runner: Runner = runner or subprocess.run

    def _run(
        self, args: list[str], env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str] | None:
        if self._hyprctl_path is None:
            return None
        try:
            return self._runner(
                [str(self._hyprctl_path), *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=dict(env),
                check=False,
            )
        except _SUBPROCESS_ERRORS as exc:
            logger.warning("host defaults: hyprctl %s failed: %s", " ".join(args), exc)
            return None

    def _json(self, args: list[str], env: Mapping[str, str]) -> Any | None:
        result = self._run(args, env)
        if result is None or result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            logger.warning(
                "host defaults: hyprctl %s returned invalid JSON: %s",
                " ".join(args),
                exc,
            )
            return None

    def _instance_signature(self, env: dict[str, str]) -> str | None:
        signature = env.get("HYPRLAND_INSTANCE_SIGNATURE")
        if signature:
            return signature
        payload = self._json(["-j", "instances"], env)
        if not isinstance(payload, list) or len(payload) != 1:
            logger.warning(
                "host defaults: expected exactly one Hyprland instance, got %s",
                len(payload) if isinstance(payload, list) else type(payload).__name__,
            )
            return None
        entry = payload[0]
        instance = entry.get("instance") if isinstance(entry, dict) else None
        if not isinstance(instance, str) or not instance:
            logger.warning("host defaults: Hyprland instance entry has no instance name")
            return None
        return instance

    @staticmethod
    def _option_value(payload: Any) -> object | None:
        if not isinstance(payload, dict):
            return None
        for key in ("int", "float"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        return None

    def read(self) -> HostSnapshot | None:
        """Return the effective delegated values, or unavailable (``None``)."""
        base_env = dict(os.environ if self._env is None else self._env)
        signature = self._instance_signature(base_env)
        if signature is None:
            return None
        env = dict(base_env)
        env["HYPRLAND_INSTANCE_SIGNATURE"] = signature

        values: dict[str, int | float] = {}
        warnings: list[str] = []
        # Several logical paths can share one compositor option (the panel and
        # the card/control/cell radii all follow decoration:rounding). Read each
        # option once and project it onto every path that names it.
        option_values: dict[str, object | None] = {}
        for path, option in HOST_MAP.items():
            if option not in option_values:
                payload = self._json(["-j", "getoption", option], env)
                option_values[option] = self._option_value(payload)
            raw = option_values[option]
            if raw is None:
                warnings.append(f"{path}: {option} is unavailable or non-scalar")
                continue
            try:
                values[path] = validate_host_value(path, raw)
            except ValueError as exc:
                warnings.append(str(exc))
        for warning in warnings:
            logger.warning("host defaults: %s", warning)
        return HostSnapshot(
            values=values,
            source=HOST_SOURCE,
            version=HOST_MAP_VERSION,
            instance_signature=signature,
            warnings=tuple(warnings),
        )
