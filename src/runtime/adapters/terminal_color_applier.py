"""Terminal palette reload adapter — OSC 4/10/11/12 channel (AD-17, FR-6, R5).

Implements ``IDesktopReloader``: after the swap repoints
``current/colors.sequences`` (AD-17 consumer wiring, through the runtime
seeder), the adapter reads the artifact's bytes and writes them UNMODIFIED
to the controlling terminal ``/dev/tty``, once per ``reload()``
invocation (shared-data-contract swap step 5: "Terminal palette applied
once from ``current/colors.sequences``").

Single source of truth: the OSC bytes are NOT derived here. The pinned
``colors.sequences.j2`` template + its ``JinjaTemplateRenderer`` binary
post-processing (``]`` → ``\\x1b]``, ``\\`` → ``\\x1b\\\\``) produce the
artifact — 16 × ``ESC]4;{i};{#rrggbb}ESC\\\\`` in ANSI-slot order, then
``ESC]10;{foreground}ESC\\\\``, ``ESC]11;{background}ESC\\\\``,
``ESC]12;{cursor}ESC\\\\``, each with a trailing LF (the file ends with
LF). This adapter is a pure READER of those bytes. New shells cat the
SAME artifact (``.zshrc`` — its repoint to the runtime state root is
gt-3-2; until then it still reads provisioning's rendered copy, which
the runtime never edits, AD-5), so the live terminal and fresh shells
theme from one byte sequence. No additional OSC codes are invented and
no terminfo negotiation occurs; the LFs are part of what the pinned
template produces (csg's own ``terminal_applier._emit_to_terminal``
writes the file bytes verbatim — this adapter mirrors that precedent).

AD-15 no-import note: ``color_scheme_generator`` is in the forbidden
import set (ARCHITECTURE-SPINE.md AD-15). This adapter MIRRORS the
``/dev/tty`` write mechanics of csg's ``adapters/terminal_applier.py``
(``open("/dev/tty", "wb")`` + flush) but NEVER imports it or its
templates. The last re-derivation coupling to csg's rendered shapes is
gone — the applier imports nothing new and touches nothing under the
install spine (AD-5/AD-11: it only reads ``current/`` and writes the
TTY).

Minimal corrupt-artifact guard (R5): the artifact must be non-empty AND
start with ``\\x1b]`` (the template's first byte by construction);
anything else is a warning + ``False`` with NO TTY write. This keeps
corrupt/tampered cache artifacts surfaced without re-implementing a
grammar — artifact content integrity beyond this is the cache's domain
(entries are hash-addressed and produced by the pinned template).

AC-4-before-AC-3 precedence (family invariant, ``hyprpaper_reloader.py``
guard order): the vacuous check runs FIRST — ``current/`` absent →
``True``; ``current/colors.sequences`` entry absent (``not exists() and
not is_symlink()``) → ``True``, WITHOUT touching the TTY. Dangling/
corrupt entry and no-controlling-terminal-with-a-valid-entry are
surfaced ``False`` failures that run only after a consumer entry exists
(R5; rt-2-5 review D2 resolution). A missing consumer entry wins over
the missing-TTY rule.

R5 headless decision: no ``sys.stdout.isatty()`` precondition — csg
guards on it because csg's stdout may be captured mid-pipeline; the
runtime adapter's contract is a SURFACED failure (spec-literal R5: the
terminal cannot be re-themed → ``False`` with ``TerminalColorApplier``
in ``ReconcileResult.reload_failures``). Attempting the write
unconditionally, wrapped in the mandated exception tuple
``(FileNotFoundError, PermissionError, OSError, ValueError)``, is what
makes a missing/headless TTY observable. The only vacuous success is "no
colors.sequences consumer entry at all".

Known Phase-2 limitations: this adapter recolors the LIVE terminal only.
A NEW shell still reads provisioning's rendered ``generated/palettes/
colors.sequences`` (``.zshrc.j2:30`` cat, provisioning-owned — the
runtime never edits provisioning-rendered configs, AD-5); repointing
that to the runtime state root is gt-3-2. No daemon, no
per-terminal-emulator config writer (none exist in the spine).

References: [consumer-wiring.md:34], [shared-data-contract.md swap step
5], [ARCHITECTURE-SPINE.md AD-15], [R5], [FR-6].
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from runtime.ports.desktop_reloader import IDesktopReloader

logger = logging.getLogger(__name__)


def _resolve_state_root(state_root: Path | None) -> Path:
    """Resolve the runtime state root (absolute).

    Mirrors ``cli/main.py`` ``_resolve_state_root`` semantics:
    ``$XDG_STATE_HOME/dotfiles`` (default ``~/.local/state/dotfiles``),
    expanded and resolved to an absolute path. An explicit ``state_root``
    (tests, composition root) is used as-is after the same expansion.
    """
    if state_root is not None:
        return state_root.expanduser().resolve()
    xdg_state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return (xdg_state / "dotfiles").expanduser().resolve()


class TerminalColorApplier(IDesktopReloader):
    """Adapter that applies the terminal palette to the live terminal.

    Reads ``state_root/current/colors.sequences`` (FS authority, NFR-3 —
    the reload loop passes no state) and writes the artifact bytes
    VERBATIM — the pinned template's OSC 4/10/11/12 payload, per-line
    trailing LFs included — to ``/dev/tty`` (or the injected ``tty_path``
    seam in tests) exactly once per ``reload()``.

    Args:
        state_root: explicit runtime state root. When ``None``, resolved
            to ``$XDG_STATE_HOME/dotfiles`` (default ``~/.local/state/
            dotfiles``). The CLI composition root passes the SAME
            state_root the reconcile use case writes.
        tty_path: the apply TARGET. When ``None``, defaults to
            ``/dev/tty``. The injected value is the sanctioned test seam
            for both unit and integration layers — the real ``/dev/tty``
            is never opened in tests.
    """

    def __init__(self, state_root: Path | None = None, tty_path: Path | None = None) -> None:
        self._state_root: Path = _resolve_state_root(state_root)
        self._tty_path: Path = tty_path if tty_path is not None else Path("/dev/tty")

    def reload(self) -> bool:
        """Read ``current/colors.sequences`` and apply the palette to the terminal.

        Returns:
            True when the palette was applied successfully, or vacuously
            when ``current/colors.sequences`` does not exist (nothing to
            apply). False on a dangling symlink, a corrupt artifact
            (empty, or not starting with the OSC introducer ``ESC]``), an
            unreadable file, or a ``/dev/tty`` open/write failure — each
            logged with the cause (surfaced failure, per R5; the
            reconcile use case collects the class name into
            ``ReconcileResult.reload_failures``).

            Precedence (family invariant): the AC-4 vacuous state wins
            over the missing-TTY rule — a missing consumer entry returns
            ``True`` without touching the TTY; the TTY checks run only
            AFTER a consumer entry exists.
        """
        current_dir = self._state_root / "current"
        if not current_dir.is_dir():
            logger.debug("no current/ dir under %s; no terminal palette to apply", self._state_root)
            return True
        link = current_dir / "colors.sequences"
        if not link.exists() and not link.is_symlink():
            logger.debug("no colors.sequences entry in %s; nothing to apply", current_dir)
            return True
        if link.is_symlink() and not link.exists():
            logger.warning("TerminalColorApplier reload failed: dangling symlink %s", link)
            return False
        try:
            payload = link.read_bytes()
        except OSError as exc:
            logger.warning("TerminalColorApplier reload failed: cannot read %s: %s", link, exc)
            return False
        if not payload or not payload.startswith(b"\x1b]"):
            logger.warning(
                "TerminalColorApplier reload failed: colors.sequences is corrupt"
                " (not OSC sequences): %s",
                link,
            )
            return False
        try:
            with open(self._tty_path, "wb") as tty:
                tty.write(payload)
                tty.flush()
        except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
            logger.warning(
                "TerminalColorApplier reload failed: cannot write palette to %s: %s",
                self._tty_path,
                exc,
            )
            return False
        logger.debug(
            "TerminalColorApplier: applied %d-byte palette to %s", len(payload), self._tty_path
        )
        return True
