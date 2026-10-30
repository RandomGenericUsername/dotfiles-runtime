# Ticket index

> Counting rule: completeness checks cover item files (`tickets/<id>-<slug>/ticket.md`) only.
> `tickets/_template/` (and any `tickets/_fixtures/`) are excluded from INDEX completeness counting.
> Entry format: link — title | `status` | execution links. Epics add `done/total children done`, done-when, and one nested line per child. Bugs add severity. Spikes add timebox state.

## Requirements

- [REQ-006-palette-vocabulary-contract](./REQ-006-palette-vocabulary-contract/ticket.md) — Palette vocabulary contract (`palette.v1`): declared roles, conformance validation, probe tests | `in-progress`
- [REQ-007-seam-cleanup](./REQ-007-seam-cleanup/ticket.md) — Seam cleanup: delete the icon-contrast guard, retire the CSG producer | `proposed` | depends: REQ-006 + assets req-008
- [REQ-004-producer-config](./REQ-004-producer-config/ticket.md) — Runtime-owned palette producer config (no ambient-env authority) | `done` | tag: v0.4.1
- [REQ-005-tpr-container-runtime](./REQ-005-tpr-container-runtime/ticket.md) — TprAdapter uses the container runtime; no host tpr-native dependency | `done` | tag: v0.4.1
- [REQ-003-palette-producer-seam](./REQ-003-palette-producer-seam/ticket.md) — Palette producer seam: adopt tpr behind the CSG default | `done` | bmad: architecture-theme-producer | worktree: .worktrees/REQ-003/ | branch: ticket/REQ-003-palette-producer-seam
- [REQ-001-repo-birth](./REQ-001-repo-birth/ticket.md) — Birth `dotfiles-runtime` as its own mu-strict repo | `done` | openspec: 2026-10-23-repo-birth | tag: v0.1.0
- [REQ-002-ags-app-refresh](./REQ-002-ags-app-refresh/ticket.md) — Manifest-driven palette refresh: discovery, ledger, lanes and launch lock | `done` | worktree: .worktrees/REQ-002/ | branch: ticket/REQ-002-ags-app-refresh | fix: d2a220b

## Bugs

- [BUG-001-required-palette](./BUG-001-required-palette/ticket.md) — Palette is mandatory: fail before the visible swap, refuse null on reconcile | `done` | severity: high | tag: v0.2.0
- [BUG-002-csg-adapter-timeout-cleanup](./BUG-002-csg-adapter-timeout-cleanup/ticket.md) — csg adapter timeout: clean up the container and never discard a complete palette | `done` | severity: medium | tag: v0.2.1
- [BUG-003-icon-contrast-prefs-malformed](./BUG-003-icon-contrast-prefs-malformed/ticket.md) — icon-contrast prefs: the loader rejects its own canonical file at 4+ entries | `done` | severity: low | worktree: .worktrees/BUG-003/ | tag: v0.2.2
- [BUG-004-btop-studio-restart](./BUG-004-btop-studio-restart/ticket.md) — btop-theme-studio is restarted by AgsReloader and loses unsaved edits | `done` | severity: high | worktree: .worktrees/BUG-004/ | tag: v0.2.1 (kept in v0.2.2)
- [BUG-005-watch-health-optional-root](./BUG-005-watch-health-optional-root/ticket.md) — optional `desired.json` with no parent marks the whole watch set degraded | `done` | severity: low | worktree: .worktrees/BUG-005/ | tag: v0.2.3
- [BUG-006-ags-restart-liveness](./BUG-006-ags-restart-liveness/ticket.md) — AGS restart is not idempotent: async quit, shared bundle and exit-0 misclassified | `done` | severity: high | worktree: .worktrees/REQ-002/ | fix: e87ad4f
- [BUG-007-palette-missing-artifact](./BUG-007-palette-missing-artifact/ticket.md) — Palette step intermittently fails with missing colors.yaml under the AGS selector | `done` | severity: high | tag: v0.4.1 | root cause: CSG CWD templates | live diagnostics verified
