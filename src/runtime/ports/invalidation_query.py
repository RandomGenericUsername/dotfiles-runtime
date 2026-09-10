from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping

from runtime.domain.invalidation import DerivationLayer


class IInvalidationQuery(ABC):
    """Port for Phase 3 invalidation-awareness (AD-21, AD-25).

    Answers "which derivation layers are stale?" by hash-compare only:
    recompute spine input hashes (templates, catalog, mappings) with the Phase 2
    canonicalization, compare against ``meta.json``, close under cascade.
    No new hash formulas, no cache-layout change. Implementations perform I/O
    in ``adapters/``; the stale-set computation itself stays domain-pure
    (``runtime.domain.invalidation``).
    """

    @abstractmethod
    def recompute_input_hashes(self) -> Mapping[DerivationLayer, str | None]:
        """Recompute current spine input hashes, keyed by derivation layer.

        A ``None`` value means the input is absent and must fail loud as
        stale downstream (never served as fresh).
        """

    @abstractmethod
    def compare_against_meta(
        self,
        recorded: Mapping[DerivationLayer, str | None],
        recomputed: Mapping[DerivationLayer, str | None],
    ) -> frozenset[DerivationLayer]:
        """Return the cascade-closed stale set for recorded vs recomputed inputs."""

    @abstractmethod
    def stale_set_with_cascade(
        self,
        directly_stale: frozenset[DerivationLayer]
        | set[DerivationLayer]
        | Collection[DerivationLayer],
    ) -> frozenset[DerivationLayer]:
        """Close a directly-stale layer set under the cascade rule."""
