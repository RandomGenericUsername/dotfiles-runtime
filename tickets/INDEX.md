# Ticket index

> Counting rule: completeness checks cover item files (`tickets/<id>-<slug>/ticket.md`) only.
> `tickets/_template/` (and any `tickets/_fixtures/`) are excluded from INDEX completeness counting.
> Entry format: link — title | `status` | execution links. Epics add `done/total children done`, done-when, and one nested line per child. Bugs add severity. Spikes add timebox state.

## Requirements

- [REQ-001-repo-birth](./REQ-001-repo-birth/ticket.md) — Birth `dotfiles-runtime` as its own mu-strict repo | `done` | openspec: 2026-10-23-repo-birth | tag: v0.1.0

## Bugs

- [BUG-001-required-palette](./BUG-001-required-palette/ticket.md) — Palette is mandatory: fail before the visible swap, refuse null on reconcile | `done` | severity: high | tag: v0.2.0
- [BUG-002-csg-adapter-timeout-cleanup](./BUG-002-csg-adapter-timeout-cleanup/ticket.md) — csg adapter timeout: clean up the container and never discard a complete palette | `done` | severity: medium | tag: v0.2.1
- [BUG-003-icon-contrast-prefs-malformed](./BUG-003-icon-contrast-prefs-malformed/ticket.md) — icon-contrast prefs: the loader rejects its own canonical file at 4+ entries | `done` | severity: low | worktree: .worktrees/BUG-003/ | tag: v0.2.2
- [BUG-004-btop-studio-restart](./BUG-004-btop-studio-restart/ticket.md) — btop-theme-studio is restarted by AgsReloader and loses unsaved edits | `done` | severity: high | worktree: .worktrees/BUG-004/ | tag: v0.2.1 (kept in v0.2.2)
