from __future__ import annotations

from abc import ABC, abstractmethod

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
        """Return the pinned ConsumerPointer table in contract order."""

    @abstractmethod
    def rules(self) -> ConsumerPointerRules:
        """Return the pinned consumer-pointer rules."""
