---
id: REQ-002
title: "Manifest-driven palette refresh: discovery, ledger, lanes and launch lock"
kind: requirement
status: proposed
lane: 3
links:
  openspec: none
  bmad: none
worktree: none
branch: none
---

# Manifest-driven palette refresh: discovery, ledger, lanes and launch lock

## Requirement

Implement the runtime side of the standalone AGS app contract from
`ags-apps` EPIC-002 / REQ-008:

- discover every deployed config link and read its `app.manifest.json`; validate
  identity (`instance == app.tsx == ags list`, `configDir == link`), reject
  invalid/duplicate manifests as structured failures, ignore dev instances, and
  resolve a name owned by the wrong process as a conflict;
- one applied-appearance ledger under `$XDG_STATE_HOME/dotfiles/` recording the
  appearance hash and per-app `applied|deferred|failed|skipped` results; one
  runtime operation lock; retry failed/deferred apps on the next trigger; cover
  palette, icons/effects and `regenerate`;
- invoke `ags request palette-refresh <appearance-hash> -i <instance>` for live
  apps with a 5 s timeout, one JSON string ack, parallel lane, runtime-owned JSONL
  logs and structured `reload_failures`;
- include configured restart apps even when down; use the safe serialized restart
  procedure (BUG-006) and the shared per-user AGS launch lock;
- migrate the clipboard daemon's config/state/cache paths from `hypr-pano` to the
  `clipboard` namespace with a one-time migration;
- no-op appearance changes do nothing; `wallpaper.state` remains the progress/UI
  event, not the refresh trigger.

## Acceptance

- [ ] Discovery/identity/failure tests cover deployed links, missing/invalid
      manifests, duplicate names, foreign/dev owners and down restart apps.
- [ ] The ledger persists applied/deferred/failed/skipped per app, is atomic,
      and retries deferred/failed on the next trigger; the operation lock
      serializes daemon/CLI/set.
- [ ] Live lane: parallel, 5 s timeout, JSON string ack, applied/deferred/busy/
      failed fixtures, no immediate retry, runtime-owned JSONL logs.
- [ ] `reload_failures` is structured with a string-compatible notification view.
- [ ] Restart lane: serialized, confirmed quit, configured down app recovery,
      shared launch lock, exit-0 delegation semantics, one wrapper per instance.
- [ ] Clipboard daemon data paths migrate once from `hypr-pano`; history and
      preferences survive; old paths are removed after the pilot.
- [ ] No-op/unchanged appearance performs no refresh/restart and does not add a
      Theme provider.
- [ ] The `wallpaper.state` description/consumer tests reflect its UI-only role.
- [ ] `make gates` is green; integration proofs use fake AGS/apps, no GUI.

## Dependencies

- `contracts` REQ-009 schemas/protocol.
- `ags-platform` REQ-012 helper and BUG-001 Theme fix.
- `dotfiles-runtime` BUG-006 restart liveness/launch lock.
- `dotfiles-v3` app list/program tuple; `dotfiles-provision` install topology.
- `ags-apps` EPIC-002 / REQ-008 spine.

## Evidence

- `ags-apps/_bmad-output/initiative-ags-standalone-apps/architecture-standalone-app-contract/architecture-standalone-app-contract.md`
  (AD-26…AD-37).
- `ags-apps/tickets/SPIKE-001-ags-refresh-patterns/ticket.md`.
- Current `src/runtime/adapters/ags_reloader.py`,
  `src/runtime/application/reconcile.py`, `clipboard_config.py`,
  `json_history_store.py`.
