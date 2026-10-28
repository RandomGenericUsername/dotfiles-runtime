"""Palette-producer composition root.

``RUNTIME__PALETTE__PRODUCER`` selects the adapter the runtime composes:
``csg`` (default, unchanged keys/behaviour) or ``tpr`` (opt-in sibling).
The selector is validated here — before any cache read — so an unknown value
fails loud at the composition root instead of silently falling back.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from runtime.adapters.tpr_adapter import (
    PALETTE_PRODUCER_ENV,
    TPR_DEFAULT_ENGINE,
    TPR_ENGINE_ENV,
)
from runtime.application.derive import find_templates_dir
from runtime.ports.color_scheme_generator import IColorSchemeGenerator

DEFAULT_PALETTE_PRODUCER: Final[str] = "csg"
VALID_PALETTE_PRODUCERS: Final[tuple[str, ...]] = ("csg", "tpr")

#: Install-spine path provisioning materializes tpr's templates into.
TPR_TEMPLATES_SPINE_PATH: Final[tuple[str, ...]] = (
    "config",
    "theme-producer",
    "templates",
)


def palette_producer_name(env: Mapping[str, str] | None = None) -> str:
    """Return the selected producer, validating ``RUNTIME__PALETTE__PRODUCER``.

    Absent/blank -> ``csg``. Unknown values raise ``ValueError`` naming the
    accepted set; this is called by the composition roots before any cache
    read so a typo cannot silently use the wrong producer.
    """
    source = os.environ if env is None else env
    raw = source.get(PALETTE_PRODUCER_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_PALETTE_PRODUCER
    value = raw.strip()
    if value not in VALID_PALETTE_PRODUCERS:
        accepted = "|".join(VALID_PALETTE_PRODUCERS)
        raise ValueError(
            f"unknown palette producer {value!r}: {PALETTE_PRODUCER_ENV} must be "
            f"one of {accepted}"
        )
    return value


def tpr_templates_dir(install_spine: Path) -> Path:
    """Install-spine tpr templates path (existence is the adapter's concern)."""
    return install_spine.joinpath(*TPR_TEMPLATES_SPINE_PATH)


def create_palette_producer(
    install_spine: Path, env: Mapping[str, str] | None = None
) -> IColorSchemeGenerator:
    """Compose the selected ``IColorSchemeGenerator`` for a composition root.

    Default (``csg``) keeps the existing spine discovery and adapter exactly.
    ``tpr`` resolves ``config/theme-producer/templates`` from the install spine
    and reads its engine from ``RUNTIME__PALETTE__TPR_ENGINE``.
    """
    source = os.environ if env is None else env
    producer = palette_producer_name(source)
    if producer == "csg":
        # Function-local so tests can still monkeypatch the adapter class.
        from runtime.adapters.csg_adapter import CsgAdapter

        return CsgAdapter(templates_dir=find_templates_dir(install_spine))

    from runtime.adapters.tpr_adapter import TprAdapter

    engine = (source.get(TPR_ENGINE_ENV) or TPR_DEFAULT_ENGINE).strip() or TPR_DEFAULT_ENGINE
    return TprAdapter(
        templates_dir=tpr_templates_dir(install_spine),
        engine=engine,
    )
