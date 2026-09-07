"""Static consumer-path spec — the pinned ConsumerPointer table (gt-4.2).

The table is contract data shared with ``shared-data-contract.md``
(gt-4.2 transcribes it verbatim — coordinate, never re-decide):

    ags     → {install}/config/ags/colors.css     → current/colors.gtk.css
    gtk-3.0 → {install}/config/gtk-3.0/colors.css → current/colors.gtk.css
    gtk-4.0 → {install}/config/gtk-4.0/colors.css → current/colors.adw.css

Adding a consumer = ONE spec line + its ``@import`` — never adapter code.
The ``config/gtk-{3,4}.0/`` spine dirs arrive in Story gt-3-1; until then
the seeder loop's missing-parent rule keeps those pointers skip+warn
(never mkdir into the spine, never dangling).
"""

from __future__ import annotations

from runtime.domain.models import ConsumerPointer, ConsumerPointerRules
from runtime.ports.consumer_path_spec import IConsumerPathSpec


class StaticConsumerPathSpec(IConsumerPathSpec):
    """Module-default spec: the pinned 3-entry table + default rules."""

    def consumer_pointers(self) -> tuple[ConsumerPointer, ...]:
        """Return the pinned table in contract order (ags, gtk-3.0, gtk-4.0)."""
        return (
            ConsumerPointer(path="config/ags/colors.css", target="colors.gtk.css"),
            ConsumerPointer(path="config/gtk-3.0/colors.css", target="colors.gtk.css"),
            ConsumerPointer(path="config/gtk-4.0/colors.css", target="colors.adw.css"),
        )

    def rules(self) -> ConsumerPointerRules:
        """Return the pinned default rules (all True)."""
        return ConsumerPointerRules()
