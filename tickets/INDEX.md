# Ticket index

> Counting rule: completeness checks cover item files (`tickets/<id>-<slug>/ticket.md`) only.
> `tickets/_template/` (and any `tickets/_fixtures/`) are excluded from INDEX completeness counting.
> Entry format: link — title | `status` | execution links. Epics add `done/total children done`, done-when, and one nested line per child. Bugs add severity. Spikes add timebox state.

## Requirements

- [REQ-021-wlogout-skin-runtime](./REQ-021-wlogout-skin-runtime/ticket.md) — wlogout skin consumer runtime regression (W-2.3/W-2.4) | `done` | worktree: .worktrees/REQ-021/ | branch: ticket/REQ-021-wlogout-skin-runtime | tag: v1.0.7
- [REQ-022-host-defaults](./REQ-022-host-defaults/ticket.md) — Host defaults: Hyprland adapter, effective precedence, `appearance.v3` and generation host identity | `in-progress` | worktree: .worktrees/REQ-022/ | branch: ticket/REQ-022-host-defaults
- [REQ-020-generations-v3](./REQ-020-generations-v3/ticket.md) — Generations v3: one committed generation and one `converge(intent)` | `done` | worktree: .worktrees/REQ-020/ | branch: ticket/REQ-020-generations-v3 | tag: v1.0.0
- [REQ-019-palette-mode-intent](./REQ-019-palette-mode-intent/ticket.md) — Desired palette mode (dark/light/smart) -> tpr settings env -> palette cache identity | `done` | worktree: .worktrees/REQ-019/ | branch: ticket/REQ-019-palette-mode-intent | tag: v0.9.5
- [REQ-018-systemd-aware-restart](./REQ-018-systemd-aware-restart/ticket.md) — Systemd-aware AGS restart lane and the v2 pointer cutover | `done` | worktree: .worktrees/REQ-018/ | branch: ticket/REQ-018-systemd-aware-restart | tag: v0.9.3, v0.9.4
- [REQ-017-supervision-doctor](./REQ-017-supervision-doctor/ticket.md) — Doctor supervision checks for the always-on AGS systemd --user units | `done` | worktree: .worktrees/REQ-017/ | branch: ticket/REQ-017-supervision-doctor | tag: v0.9.2
- [REQ-016-appearance-v2-manifests](./REQ-016-appearance-v2-manifests/ticket.md) — Consume the real v2 appearance manifests (TOML, nested groups, modes) | `done` | worktree: .worktrees/REQ-016/ | branch: ticket/REQ-016-appearance-v2-manifests | tag: v0.9.1
- [REQ-015-appearance-v1-pointer-removal](./REQ-015-appearance-v1-pointer-removal/ticket.md) — Remove the v1 skin pointers after the appearance v2 cutover | `done` | folded into REQ-018
- [REQ-014-appearance-plane](./REQ-014-appearance-plane/ticket.md) — Publish one composed appearance generation behind one atomically repointed pointer | `done` | worktree: .worktrees/REQ-014/ | branch: ticket/REQ-014-appearance-plane | tag: v0.9.0
- [REQ-013-skin-pointers-all-apps](./REQ-013-skin-pointers-all-apps/ticket.md) — Skin consumer pointers for every skin target, derived from artifacts (not AGS-only) | `done` | worktree: .worktrees/REQ-013/ | branch: ticket/REQ-013-skin-pointers-all-apps | tag: v0.8.2
- [REQ-012-skins-hairline-token](./REQ-012-skins-hairline-token/ticket.md) — Accept the skins v0.6.0 `shape.hairline-w` token | `done` | worktree: .worktrees/REQ-012/ | branch: ticket/REQ-012-skins-hairline-token | tag: v0.8.1
- [REQ-011-skins-interface-reconcile](./REQ-011-skins-interface-reconcile/ticket.md) — Reconcile the skin expander to the canonical skin interface | `done` | worktree: .worktrees/REQ-011/ | branch: ticket/REQ-011-skins-interface-reconcile
- [REQ-010-skins-derivation-layer](./REQ-010-skins-derivation-layer/ticket.md) — Skins derivation layer: palette-independent per-app CSS + token map | `done` | worktree: .worktrees/REQ-010/ | branch: ticket/REQ-010-skins-derivation-layer | openspec: skins-derivation-layer
- [REQ-008-dotfiles-user-settings](./REQ-008-dotfiles-user-settings/ticket.md) — User-owned dotfiles settings with a terminal ANSI policy (`wallpaper` default vs `themed`) | `proposed` | worktree: .worktrees/REQ-008/ | branch: ticket/REQ-008-dotfiles-user-settings
- [REQ-009-shared-monitor-catalog](./REQ-009-shared-monitor-catalog/ticket.md) — Shared monitor catalog service: `dotfiles-runtime monitors list --format json` | `done` | worktree none, branch main (merged 2026-11-04; worktree/branch removed)
- [REQ-006-palette-vocabulary-contract](./REQ-006-palette-vocabulary-contract/ticket.md) — Palette vocabulary contract (`palette.v1`): declared roles, conformance validation, probe tests | `done` | tag: v0.5.0 | live: conforming 51 roles
- [REQ-007-seam-cleanup](./REQ-007-seam-cleanup/ticket.md) — Seam cleanup: delete the icon-contrast guard, retire the CSG producer | `done` | tag: v0.5.1 | live: no guard, producer tpr-only
- [REQ-004-producer-config](./REQ-004-producer-config/ticket.md) — Runtime-owned palette producer config (no ambient-env authority) | `done` | tag: v0.4.1
- [REQ-005-tpr-container-runtime](./REQ-005-tpr-container-runtime/ticket.md) — TprAdapter uses the container runtime; no host tpr-native dependency | `done` | tag: v0.4.1
- [REQ-003-palette-producer-seam](./REQ-003-palette-producer-seam/ticket.md) — Palette producer seam: adopt tpr behind the CSG default | `done` | bmad: architecture-theme-producer | worktree: .worktrees/REQ-003/ | branch: ticket/REQ-003-palette-producer-seam
- [REQ-001-repo-birth](./REQ-001-repo-birth/ticket.md) — Birth `dotfiles-runtime` as its own mu-strict repo | `done` | openspec: 2026-10-23-repo-birth | tag: v0.1.0
- [REQ-002-ags-app-refresh](./REQ-002-ags-app-refresh/ticket.md) — Manifest-driven palette refresh: discovery, ledger, lanes and launch lock | `done` | worktree: .worktrees/REQ-002/ | branch: ticket/REQ-002-ags-app-refresh | fix: d2a220b

## Bugs

- [BUG-016-doctor-stable-appearance-link](./BUG-016-doctor-stable-appearance-link/ticket.md) — Doctor flags the legitimate stable wlogout `appearance.css` link as a retired v2 convenience link | `done` | severity: high | worktree: .worktrees/BUG-016/ | branch: ticket/BUG-016-doctor-stable-appearance-link | tag: v1.0.8
- [BUG-015-one-lock-seed-consolidation](./BUG-015-one-lock-seed-consolidation/ticket.md) — First-run seed and icons regenerate still use a second `.seed.lock` instead of the one `converge.lock` | `done` | severity: high | worktree: .worktrees/BUG-015/ | branch: ticket/BUG-015-one-lock-seed-consolidation | tag: v1.0.6
- [BUG-014-gc-current-layer-protection](./BUG-014-gc-current-layer-protection/ticket.md) — Converge GC deletes the committed current generation's palette/wallpaper/appearance cache layers | `done` | severity: critical | worktree: .worktrees/BUG-014/ | branch: ticket/BUG-014-gc-current-layer-protection | tag: v1.0.5
- [BUG-013-reactive-history-cli-race](./BUG-013-reactive-history-cli-race/ticket.md) — Reactive mode changes leave a stale history tail, converge slowly, and `reconcile --regenerate-stale` returns a pre-commit snapshot | `done` | severity: high | worktree: .worktrees/BUG-013/ | branch: ticket/BUG-013-reactive-history-cli-race | tag: v1.0.4
- [BUG-012-doctor-v3-generation-identity](./BUG-012-doctor-v3-generation-identity/ticket.md) — Doctor supervision leg compares v2 appearance hashes against the v3 ledger and reports false drift | `done` | severity: high | worktree: .worktrees/BUG-012/ | branch: ticket/BUG-012-doctor-v3-generation-identity | tag: v1.0.3
- [BUG-011-ags-restart-systemd-wait](./BUG-011-ags-restart-systemd-wait/ticket.md) — Bar restart uses `systemctl restart --wait`, which blocks until the service stops again | `done` | severity: high | worktree: .worktrees/BUG-011/ | branch: ticket/BUG-011-ags-restart-systemd-wait | tag: v1.0.2
- [BUG-010-converge-job-trigger-payload](./BUG-010-converge-job-trigger-payload/ticket.md) — Daemon converge jobs write an invalid history trigger (`daemon`) and `wallpaper.state` can exceed/stage-violate the event contract | `done` | severity: high | worktree: .worktrees/BUG-010/ | branch: ticket/BUG-010-converge-job-trigger-payload | tag: v1.0.1
- [BUG-009-release-version-metadata](./BUG-009-release-version-metadata/ticket.md) — v0.6.0 release metadata still reports 0.5.2; provisioning verify fails | `done` | severity: high | worktree: .worktrees/BUG-009/ | branch: ticket/BUG-009-release-version-metadata | fix: v0.6.1
- [BUG-008-bare-ags-run-discovery](./BUG-008-bare-ags-run-discovery/ticket.md) — Bar refresh fails: discovery skips bare `ags run` | `done` | severity: high | tag: v0.5.2 | fix: 8ef66b1 | live proof 2026-11-04
- [BUG-001-required-palette](./BUG-001-required-palette/ticket.md) — Palette is mandatory: fail before the visible swap, refuse null on reconcile | `done` | severity: high | tag: v0.2.0
- [BUG-002-csg-adapter-timeout-cleanup](./BUG-002-csg-adapter-timeout-cleanup/ticket.md) — csg adapter timeout: clean up the container and never discard a complete palette | `done` | severity: medium | tag: v0.2.1
- [BUG-003-icon-contrast-prefs-malformed](./BUG-003-icon-contrast-prefs-malformed/ticket.md) — icon-contrast prefs: the loader rejects its own canonical file at 4+ entries | `done` | severity: low | worktree: .worktrees/BUG-003/ | tag: v0.2.2
- [BUG-004-btop-studio-restart](./BUG-004-btop-studio-restart/ticket.md) — btop-theme-studio is restarted by AgsReloader and loses unsaved edits | `done` | severity: high | worktree: .worktrees/BUG-004/ | tag: v0.2.1 (kept in v0.2.2)
- [BUG-005-watch-health-optional-root](./BUG-005-watch-health-optional-root/ticket.md) — optional `desired.json` with no parent marks the whole watch set degraded | `done` | severity: low | worktree: .worktrees/BUG-005/ | tag: v0.2.3
- [BUG-006-ags-restart-liveness](./BUG-006-ags-restart-liveness/ticket.md) — AGS restart is not idempotent: async quit, shared bundle and exit-0 misclassified | `done` | severity: high | worktree: .worktrees/REQ-002/ | fix: e87ad4f
- [BUG-007-palette-missing-artifact](./BUG-007-palette-missing-artifact/ticket.md) — Palette step intermittently fails with missing colors.yaml under the AGS selector | `done` | severity: high | tag: v0.4.1 | root cause: CSG CWD templates | live diagnostics verified

## Ops tasks

- [OPS-001-ticket-status-sync](./OPS-001-ticket-status-sync/ticket.md) — Sync BUG-008 ticket state with its shipped release | `done` | worktree: .worktrees/OPS-001/ | branch: ticket/OPS-001-ticket-status-sync
