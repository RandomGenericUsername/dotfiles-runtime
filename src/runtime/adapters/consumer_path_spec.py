"""Static consumer-path spec — the pinned ConsumerPointer table (gt-4.2).

The table is contract data shared with ``shared-data-contract.md``
(gt-4.2 transcribes it verbatim — coordinate, never re-decide):

    ags     → {install}/config/ags/colors.css     → current/colors.gtk.css
    gtk-3.0 → {install}/config/gtk-3.0/colors.css → current/colors.gtk.css
    gtk-4.0 → {install}/config/gtk-4.0/colors.css → current/colors.adw.css
    rofi    → {install}/config/rofi/colors.rasi   → current/colors.rasi
    btop    → {install}/config/btop/themes/colors.btop.theme →
                    current/colors.btop.theme

Adding a consumer = ONE spec line + its ``@import`` — never adapter code.
The ``config/gtk-{3,4}.0/`` spine dirs arrive in Story gt-3-1; until then
the seeder loop's missing-parent rule keeps those pointers skip+warn
(never mkdir into the spine, never dangling). The
``config/btop/themes/`` spine dir is likewise PROVISIONING's — the
btop_config role creates it (provision-btop-config); see the note there
for why btop needs real files in its own theme dir rather than a
pointer btop could read directly.
"""

from __future__ import annotations

from runtime.domain.models import ConsumerPointer, ConsumerPointerRules
from runtime.ports.consumer_path_spec import IConsumerPathSpec


class StaticConsumerPathSpec(IConsumerPathSpec):
    """Module-default spec: the pinned 5-entry table + default rules."""

    def consumer_pointers(self) -> tuple[ConsumerPointer, ...]:
        """Return the pinned table in contract order (ags, gtk-3.0, gtk-4.0,
        rofi, btop).

        The btop pointer is NOT an @import-style rename: btop's
        ``Theme::setTheme`` (btop_theme.cpp:451-471) matches ``color_theme``
        only against themes it discovered while scanning its theme dirs at
        startup, so each generated theme must physically exist under
        ``$XDG_CONFIG_HOME/btop/themes`` — reachable here because
        ``~/.config/btop`` is a symlink into the spine (config_links). The
        path basename is therefore significant and must stay
        ``colors.btop.theme`` so the rendered ``color_theme =
        "colors.btop.theme"`` is matched by filename against the discovered
        entry. A mismatch does not error — btop silently falls back to
        ``Default_theme``.
        """
        return (
            ConsumerPointer(path="config/ags/colors.css", target="colors.gtk.css"),
            ConsumerPointer(path="config/gtk-3.0/colors.css", target="colors.gtk.css"),
            ConsumerPointer(path="config/gtk-4.0/colors.css", target="colors.adw.css"),
            ConsumerPointer(path="config/rofi/colors.rasi", target="colors.rasi"),
            ConsumerPointer(
                path="config/btop/themes/colors.btop.theme",
                target="colors.btop.theme",
            ),
        )

    def rules(self) -> ConsumerPointerRules:
        """Return the pinned default rules (all True)."""
        return ConsumerPointerRules()
