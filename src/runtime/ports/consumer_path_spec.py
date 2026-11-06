from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from runtime.domain.models import ConsumerPointer, ConsumerPointerRules


class IConsumerPathSpec(ABC):
    """Declarative consumer-pointer table + rules (gt-4.2 contract pin).

    The canonical table is pinned in ``shared-data-contract.md``
    (ConsumerPointer table + rules, authored in gt-4.2); the spec is the
    AD-11 exception-class boundary definition (ARCHITECTURE-SPINE AD-11,
    Epic 4 exception extended from ONE symlink to the spec'd pointer
    class): the runtime writes under the install spine ONLY the pointer
    symlinks this spec returns, never anything else.

    Implementations return pure domain dataclasses — never concrete
    I/O, never adapter classes.
    """

    @abstractmethod
    def consumer_pointers(self) -> tuple[ConsumerPointer, ...]:
        """Return the pinned ConsumerPointer table in contract order.

        The table is intentionally fixed (palette/theme consumers). Skin
        pointers are NOT part of it: they are data-derived per generation
        through :meth:`skin_pointers`.
        """

    def skin_pointers(self, config_dirs: Iterable[str]) -> tuple[ConsumerPointer, ...]:
        """Return the skin pointer pair for every supplied config dir.

        The naming rule is contract data, not an app list: a skin target
        ``<configDir>`` consumes ``config/<configDir>/skin.css`` ->
        ``current/skins/<configDir>.css`` and
        ``config/<configDir>/skin.json`` -> ``current/skin.json``.
        Callers derive ``config_dirs`` from the current skin artifacts
        (``runtime.domain.models.skin_config_dirs``); unsafe path segments
        are rejected here so a malformed artifact can never escape
        ``config/``.
        """
        pointers: list[ConsumerPointer] = []
        for config_dir in sorted(set(config_dirs)):
            if (
                not config_dir
                or config_dir in (".", "..")
                or "/" in config_dir
                or "\\" in config_dir
                or config_dir.strip() != config_dir
            ):
                continue
            pointers.append(
                ConsumerPointer(
                    path=f"config/{config_dir}/skin.css",
                    target=f"skins/{config_dir}.css",
                )
            )
            pointers.append(
                ConsumerPointer(
                    path=f"config/{config_dir}/skin.json",
                    target="skin.json",
                )
            )
        return tuple(pointers)

    @abstractmethod
    def rules(self) -> ConsumerPointerRules:
        """Return the pinned consumer-pointer rules."""
