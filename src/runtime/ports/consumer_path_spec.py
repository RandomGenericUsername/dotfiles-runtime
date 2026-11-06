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

        The table is intentionally fixed (palette/theme consumers). The v1
        skin pointer family was retired in REQ-018; v2 consumers use
        :meth:`appearance_pointers`.
        """

    def appearance_pointers(self, config_dirs: Iterable[str]) -> tuple[ConsumerPointer, ...]:
        """Return the v2 convenience link for every supplied config dir.

        ``config/<configDir>/appearance.css`` → ``current/appearance/<configDir>.css``.
        The link target names the single repointed hop, so the link itself is
        stable across generations. Unsafe path segments are rejected.
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
                    path=f"config/{config_dir}/appearance.css",
                    target=f"appearance/{config_dir}.css",
                )
            )
        return tuple(pointers)

    @abstractmethod
    def rules(self) -> ConsumerPointerRules:
        """Return the pinned consumer-pointer rules."""
