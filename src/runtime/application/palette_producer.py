"""Palette-producer composition root.

The producer is runtime-owned, provisioning-rendered state, not caller
environment (REQ-004):

1. explicit ``--producer csg|tpr`` (one-shot override);
2. ``<install_spine>/config/dotfiles-runtime/producer.json``;
3. default ``csg`` with a warning when the file is absent.

``RUNTIME__PALETTE__PRODUCER`` / ``RUNTIME__PALETTE__TPR_ENGINE`` are never
authoritative. When present and different they are reported in
``ignored_env`` and surfaced as a warning, so a selector process that predates
provisioning cannot silently keep the old producer.

An invalid/malformed config fails loud here — before any cache read or tool
invocation.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from runtime.adapters.tpr_adapter import (
    PALETTE_PRODUCER_ENV,
    TPR_DEFAULT_ENGINE,
    TPR_DEFAULT_RUNTIME,
    TPR_ENGINE_ENV,
    VALID_TPR_RUNTIMES,
)
from runtime.application.derive import find_templates_dir
from runtime.ports.color_scheme_generator import IColorSchemeGenerator

logger = logging.getLogger(__name__)

DEFAULT_PALETTE_PRODUCER: Final[str] = "csg"
VALID_PALETTE_PRODUCERS: Final[tuple[str, ...]] = ("csg", "tpr")

#: Versioned runtime-owned producer config under the install spine.
PRODUCER_CONFIG_SCHEMA_VERSION: Final[int] = 1
PRODUCER_CONFIG_SPINE_PATH: Final[tuple[str, ...]] = (
    "config",
    "dotfiles-runtime",
    "producer.json",
)
_CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    {"schema_version", "producer", "tpr_engine", "tpr_runtime"}
)

#: Install-spine path provisioning materializes tpr's templates into.
TPR_TEMPLATES_SPINE_PATH: Final[tuple[str, ...]] = (
    "config",
    "theme-producer",
    "templates",
)

#: Provisioned tpr settings (image prefix/tag, container engine).
TPR_SETTINGS_SPINE_PATH: Final[tuple[str, ...]] = (
    "config",
    "theme-producer",
    "settings.toml",
)


@dataclass(frozen=True, slots=True)
class ProducerResolution:
    """One resolved producer decision, including its provenance.

    ``source`` is ``"cli"``, ``"config"`` or ``"default"``. ``engine`` /
    ``runtime`` are ``None`` for csg (they are meaningless there). The ambient
    maps are reported verbatim so ``inspect producer`` can show stale env that
    was deliberately ignored.
    """

    producer: str
    source: str
    engine: str | None
    runtime: str | None
    config_path: Path
    ambient_env: dict[str, str]
    ignored_env: dict[str, str]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """JSON-shaped projection for ``inspect producer --format json``."""
        return {
            "producer": self.producer,
            "source": self.source,
            "engine": self.engine,
            "runtime": self.runtime,
            "config_path": str(self.config_path),
            "ambient_env": dict(self.ambient_env),
            "ignored_env": dict(self.ignored_env),
            "warnings": list(self.warnings),
        }


def producer_config_path(install_spine: Path) -> Path:
    """Return the runtime-owned producer config path for ``install_spine``."""
    return install_spine.joinpath(*PRODUCER_CONFIG_SPINE_PATH)


def tpr_templates_dir(install_spine: Path) -> Path:
    """Install-spine tpr templates path (existence is the adapter's concern)."""
    return install_spine.joinpath(*TPR_TEMPLATES_SPINE_PATH)


def tpr_settings_path(install_spine: Path) -> Path:
    """Install-spine tpr settings path (existence is the adapter's concern)."""
    return install_spine.joinpath(*TPR_SETTINGS_SPINE_PATH)


def _validate_producer(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}: producer must be a non-empty string")
    producer = value.strip()
    if producer not in VALID_PALETTE_PRODUCERS:
        accepted = "|".join(VALID_PALETTE_PRODUCERS)
        raise ValueError(f"unknown palette producer {producer!r}: must be one of {accepted}")
    return producer


def _load_producer_config(path: Path) -> dict[str, object] | None:
    """Read + validate the versioned config; ``None`` when absent.

    Malformed JSON, a wrong ``schema_version``, an unknown producer/engine/
    runtime, unknown keys, or wrong field types raise ``ValueError`` naming
    the config path. A missing file is the documented default path, not an
    error.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"cannot read palette producer config {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid palette producer config {path}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid palette producer config {path}: expected a JSON object")

    unknown = sorted(set(data) - _CONFIG_KEYS)
    if unknown:
        raise ValueError(
            f"invalid palette producer config {path}: unknown field(s) {', '.join(unknown)}"
        )
    version = data.get("schema_version")
    if isinstance(version, bool) or version != PRODUCER_CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"invalid palette producer config {path}: schema_version must be "
            f"{PRODUCER_CONFIG_SCHEMA_VERSION}, got {version!r}"
        )
    if "producer" not in data:
        raise ValueError(f"invalid palette producer config {path}: missing 'producer'")
    try:
        _validate_producer(data["producer"], context="config")
    except ValueError as exc:
        raise ValueError(f"invalid palette producer config {path}: {exc}") from exc

    engine = data.get("tpr_engine", TPR_DEFAULT_ENGINE)
    if not isinstance(engine, str) or not engine.strip():
        raise ValueError(
            f"invalid palette producer config {path}: tpr_engine must be a non-empty string"
        )
    runtime = data.get("tpr_runtime", TPR_DEFAULT_RUNTIME)
    if not isinstance(runtime, str) or runtime.strip() not in VALID_TPR_RUNTIMES:
        accepted = "|".join(VALID_TPR_RUNTIMES)
        raise ValueError(
            f"invalid palette producer config {path}: tpr_runtime must be one of {accepted}"
        )
    return {
        "schema_version": PRODUCER_CONFIG_SCHEMA_VERSION,
        "producer": str(data["producer"]).strip(),
        "tpr_engine": engine.strip(),
        "tpr_runtime": runtime.strip(),
    }


def resolve_palette_producer(
    install_spine: Path,
    *,
    producer: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ProducerResolution:
    """Resolve the producer at operation time (CLI > config > default).

    Ambient producer/engine env is reported but never used. The config is
    always validated when present, even with an explicit one-shot override,
    so a corrupt provisioning output cannot hide behind a flag.
    """
    source_env = os.environ if env is None else env
    config_path = producer_config_path(install_spine)
    config = _load_producer_config(config_path)
    warnings: list[str] = []

    ambient_env: dict[str, str] = {}
    for key in (PALETTE_PRODUCER_ENV, TPR_ENGINE_ENV):
        if key in source_env:
            ambient_env[key] = source_env[key]

    if producer is not None:
        resolved_producer = _validate_producer(producer, context="--producer")
        source = "cli"
    elif config is not None:
        resolved_producer = str(config["producer"])
        source = "config"
    else:
        resolved_producer = DEFAULT_PALETTE_PRODUCER
        source = "default"
        warnings.append(
            f"no palette producer config at {config_path}; defaulting to {resolved_producer}"
        )

    if resolved_producer == "tpr":
        engine = str(config["tpr_engine"]) if config is not None else TPR_DEFAULT_ENGINE
        runtime = str(config["tpr_runtime"]) if config is not None else TPR_DEFAULT_RUNTIME
    else:
        engine = None
        runtime = None

    ignored_env: dict[str, str] = {}
    ambient_producer = ambient_env.get(PALETTE_PRODUCER_ENV, "").strip()
    if ambient_producer and ambient_producer != resolved_producer:
        ignored_env[PALETTE_PRODUCER_ENV] = ambient_env[PALETTE_PRODUCER_ENV]
        warnings.append(
            f"ignoring ambient {PALETTE_PRODUCER_ENV}={ambient_producer!r}; "
            f"{source} resolved {resolved_producer!r}"
        )
    ambient_engine = ambient_env.get(TPR_ENGINE_ENV, "").strip()
    if ambient_engine and ambient_engine != engine:
        ignored_env[TPR_ENGINE_ENV] = ambient_env[TPR_ENGINE_ENV]
        warnings.append(
            f"ignoring ambient {TPR_ENGINE_ENV}={ambient_engine!r}; "
            f"resolved engine is {engine!r}"
        )

    return ProducerResolution(
        producer=resolved_producer,
        source=source,
        engine=engine,
        runtime=runtime,
        config_path=config_path,
        ambient_env=ambient_env,
        ignored_env=ignored_env,
        warnings=tuple(warnings),
    )


def palette_producer_name(
    install_spine: Path | None = None,
    env: Mapping[str, str] | None = None,
    *,
    producer: str | None = None,
) -> str:
    """Return the resolved producer name (compatibility helper).

    ``install_spine`` defaults to the standard XDG location only for callers
    that genuinely have no spine; composition roots should pass the resolved
    spine explicitly.
    """
    if install_spine is None:
        xdg_data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        install_spine = xdg_data / "dotfiles"
    return resolve_palette_producer(install_spine, producer=producer, env=env).producer


def create_palette_producer(
    install_spine: Path,
    *,
    producer: str | None = None,
    env: Mapping[str, str] | None = None,
    resolution: ProducerResolution | None = None,
) -> IColorSchemeGenerator:
    """Compose the selected ``IColorSchemeGenerator`` for a composition root.

    Every CLI/daemon root calls this at operation time with the same resolver,
    so a selector's stale environment can never change the producer. ``csg``
    keeps the existing spine discovery and adapter exactly. ``tpr`` resolves
    ``config/theme-producer/templates`` and pins the provisioned
    ``config/theme-producer/settings.toml`` (image tag/container engine) when
    that file exists.
    """
    if resolution is None:
        resolution = resolve_palette_producer(install_spine, producer=producer, env=env)
    for warning in resolution.warnings:
        logger.warning("palette producer: %s", warning)

    if resolution.producer == "csg":
        # Function-local so tests can still monkeypatch the adapter class.
        from runtime.adapters.csg_adapter import CsgAdapter

        return CsgAdapter(templates_dir=find_templates_dir(install_spine))

    from runtime.adapters.tpr_adapter import TprAdapter

    settings_path = tpr_settings_path(install_spine)
    return TprAdapter(
        templates_dir=tpr_templates_dir(install_spine),
        engine=resolution.engine,
        runtime=resolution.runtime,
        settings_path=settings_path if settings_path.is_file() else None,
    )
