"""Clipboard config reader — tolerant per-kind retention from a settings file.

Settings are file-only (design D5): ``$XDG_CONFIG_HOME/hypr-pano/config.json``
(default ``~/.config/hypr-pano/config.json``). The shape is::

    { "limits": { "text": 500, "image": 25, ... } }

Parsing is deliberately tolerant: a missing file, malformed JSON, a non-object
``limits``, or a per-kind value that is not a positive integer falls back to the
built-in default for that kind and logs a warning. The watcher must keep
capturing with sane limits even when the operator's file is wrong.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path

from runtime.domain.clipboard import ITEM_TYPES, RetentionLimits
from runtime.ports.clipboard import IClipboardConfigReader

__all__ = ["DEFAULT_CONFIG_PATH", "JsonClipboardConfigReader", "default_config_path"]

logger = logging.getLogger(__name__)


def default_config_path() -> Path:
    """``$XDG_CONFIG_HOME/hypr-pano/config.json`` (falls back to ``~/.config``)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "hypr-pano" / "config.json"


#: Module-level default resolved lazily so tests can monkeypatch the env.
DEFAULT_CONFIG_PATH: Path = default_config_path()


class JsonClipboardConfigReader(IClipboardConfigReader):
    """Reads retention limits, degrading to defaults on any error."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else default_config_path()

    def read(self) -> RetentionLimits:
        """Return configured limits; never raises (defaults on any problem)."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.info("clipboard: no config at %s; using defaults", self._path)
            return RetentionLimits()
        except OSError as exc:
            logger.warning("clipboard: cannot read config %s: %s; using defaults", self._path, exc)
            return RetentionLimits()

        try:
            data = json.loads(raw)
        except ValueError as exc:
            logger.warning("clipboard: malformed config %s: %s; using defaults", self._path, exc)
            return RetentionLimits()

        if not isinstance(data, Mapping):
            logger.warning("clipboard: config %s is not an object; using defaults", self._path)
            return RetentionLimits()

        limits = data.get("limits")
        if limits is None:
            return RetentionLimits()
        if not isinstance(limits, Mapping):
            logger.warning(
                "clipboard: config %s `limits` is not an object; using defaults", self._path
            )
            return RetentionLimits()

        defaults = RetentionLimits()
        overrides: dict[str, int] = {}
        for kind in ITEM_TYPES:
            if kind not in limits:
                continue
            value = limits[kind]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                logger.warning(
                    "clipboard: config %s limit %r=%r invalid; using default %d",
                    self._path,
                    kind,
                    value,
                    defaults.for_kind(kind),
                )
                continue
            overrides[kind] = value
        return RetentionLimits(**overrides)
