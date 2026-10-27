---
id: REQ-002
title: "Manifest-driven palette refresh: discovery, ledger, lanes and launch lock"
kind: requirement
status: done
lane: 3
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-002/
branch: ticket/REQ-002-ags-app-refresh
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

- [x] Discovery/identity/failure tests cover deployed links, missing/invalid
      manifests, duplicate names, foreign/dev owners and down restart apps.
- [x] The ledger persists applied/deferred/failed/skipped per app, is atomic,
      and retries deferred/failed on the next trigger; the operation lock
      serializes daemon/CLI/set.
- [x] Live lane: parallel, 5 s timeout, JSON string ack, applied/deferred/busy/
      failed fixtures, no immediate retry, runtime-owned JSONL logs.
- [x] `reload_failures` is structured with a string-compatible notification view.
- [x] Restart lane: serialized, confirmed quit, configured down app recovery,
      shared launch lock, exit-0 delegation semantics, one wrapper per instance.
- [x] Clipboard daemon data paths migrate once from `hypr-pano`; history and
      preferences survive; old paths are removed after the pilot.
- [x] No-op/unchanged appearance performs no refresh/restart and does not add a
      Theme provider.
- [x] The `wallpaper.state` description/consumer tests reflect its UI-only role.
- [x] `make gates` is green; integration proofs use fake AGS/apps, no GUI.

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

## Triage (implementation start, 2026-10-26)

- `kind: requirement`, `lane: 3` — cross-cutting runtime slice with
  deterministic fake-AGS proofs; no OpenSpec change.
- Worktree `.worktrees/REQ-002/` on `ticket/REQ-002-ags-app-refresh`;
  BUG-006 (`e87ad4f`, commit `cca8f2e`) is already on this branch and its
  restart liveness/launch-lock behavior is preserved.
- The installed-spine contract is unambiguous: discovery is
  `<install>/config` from `_resolve_install_spine()`
  (`$XDG_DATA_HOME/dotfiles` or `DOTFILES_INSTALL_SPINE`), never a composition
  checkout (AD-26/AD-31).
- Contracts source: `contracts@5eb01b8`; new schemas/protocol copied per the
  embedded-schema + conformance-copy convention.

## Implementation plan

1. **Discovery adapter** — enumerate deployed links/manifests under the
   installed `config/` root; validate one-segment identity, `configDir == link`,
   `instance == app.tsx == ags list`, reject missing/invalid/duplicate entries,
   ignore dev instances, and report foreign owners as structured failures.
2. **Ledger + operation lock** — one atomic applied-appearance ledger under the
   state root (`applied|deferred|failed|skipped`, per-app appearance hash),
   retry non-applied entries next trigger, serialize operations with one flock.
3. **Refresh coordinator** — ledger gate; parallel live lane
   (`ags request palette-refresh <hash> -i <instance>`, 5 s, one JSON string
   ack, no immediate retry); serialized restart lane through BUG-006,
   including configured down apps; runtime JSONL records; structured
   `ReloadFailure` entries with a string view.
4. **Composition wiring** — reconcile/`wallpaper set`/`icons regenerate`
   (and therefore reactive/doctor) use the coordinator instead of the old
   unconditional AGS restart reloader.
5. **Clipboard namespace** — config/state/cache paths move to `clipboard`;
   one-time migration merges history/favorites and removes legacy paths only
   after the new copies are proven.
6. **Contracts/docs** — pin `5eb01b8`, embed the four new schemas, refresh the
   `wallpaper.state` description as progress/UI only, update README/
   ARCHITECTURE/CHANGELOG.
7. **Proofs** — deterministic fake AGS/apps/manifests; no GUI, no host AGS
   kills, no sudo; `make gates-ci` green.

## Implementation (2026-10-26)

Commits on `ticket/REQ-002-ags-app-refresh`:

- `3ec5db6` — planning/start.
- `d2a220b` — implementation (`feat(REQ-002): manifest-driven AGS refresh,
  ledger and clipboard namespace`).
- Closeout (this commit) — acceptance/evidence/index/CHANGELOG.

Files:

- `src/runtime/adapters/ags_app_discovery.py` — installed-spine discovery,
  manifest schema validation, one-segment identity, `configDir == link`,
  `instance == app.tsx == ags list`, duplicate app/instance/link failures,
  foreign/dev owner conflicts, down live skips and down restart inclusion.
- `src/runtime/adapters/appearance_ledger.py` — atomic
  `appearance-ledger.json`, per-app `applied|deferred|failed|skipped`,
  appearance hashes, one `.appearance-operation.lock` flock.
- `src/runtime/adapters/ags_app_refresh.py` — ledger gate, parallel live lane
  (`ags request palette-refresh <hash> -i <instance>`, 5 s timeout, one JSON
  string ack), serialized restart lane via BUG-006
  (`AgsReloader.restart_deployed_app`), runtime JSONL records under
  `$XDG_STATE_HOME/dotfiles/logs/ags-app-refresh/<runId>/`, structured
  failures, no immediate retry/restart fallback, mid-operation appearance
  drift downgrades applied results to deferred.
- `src/runtime/domain/ags_apps.py` — pure identity/ledger/result shapes and
  `ReloadFailure` (structured fields + `str` notification view).
- `src/runtime/ports/ags_app_refresher.py` — use-case port.
- `src/runtime/application/reconcile.py`,
  `src/runtime/application/regenerate_icons.py`, `src/runtime/cli/main.py` —
  coordinator wired into `wallpaper set`, `reconcile`, `icons regenerate`
  (and therefore reactive converge/doctor); the unconditional AGS reloader is
  removed from the ordinary reloader list.
- `src/runtime/adapters/ags_reloader.py` — additive
  `restart_deployed_app()` reuses BUG-006's quit/release/launch/name
  verification and shared launch lock; existing `reload()` behavior unchanged.
- `src/runtime/adapters/clipboard_config.py`,
  `json_history_store.py`, `wl_paste_source.py`,
  `clipboard_migration.py` — `clipboard` namespace + one-time
  `hypr-pano` migration (history merge by hash with favorites/recency,
  path rewrite, config/cache move, legacy removal after success).
- `src/runtime/adapters/CONTRACT_PIN`, `contracts/`,
  `src/runtime/adapters/schemas/`, `contract_schemas.py` — pin `5eb01b8`,
  four AGS schemas embedded/conformance-copied, protocol doc copied.
- `contracts/event-contract.{json,md}` — `wallpaper.state` is explicitly the
  progress/UI event, not the palette-refresh trigger.
- Tests: `tests/unit/test_ags_app_discovery.py` (24),
  `tests/unit/test_ags_app_refresh.py` (23), `tests/unit/test_clipboard_migration.py`
  (6), plus conformance/restart-lane/wiring assertions in existing modules.

## Red evidence (pre-implementation `cca8f2e`)

The new discovery/refresh tests collected against the pre-REQ-002 tree in a
detached worktree:

```text
$ PYTHONPATH=<red>/src:<red> python -m pytest -q \
    tests/unit/test_ags_app_discovery.py tests/unit/test_ags_app_refresh.py
E   ModuleNotFoundError: No module named 'runtime.adapters.ags_app_discovery'
E   ImportError: cannot import name 'appearance_ledger' from 'runtime.adapters'
2 errors during collection — RED_EXIT=2
```

The pre-fix tree had no manifest discovery, ledger, live lane, JSONL records or
clipboard migration; the old AGS reloader restarted every discovered process
unconditionally.

## Green evidence

Focused proof:

```text
$ uv run pytest -q tests/unit/test_ags_app_discovery.py \
    tests/unit/test_ags_app_refresh.py tests/unit/test_clipboard_migration.py
53 passed
```

Contracts pin (optional cross-repo positive check):

```text
$ CONTRACTS_REPO=/home/inumaki/Development/dotfiles/contracts \
    python3 scripts/contract-pin-check.py
contract-pin-check: PASS — vendored bindings match \
  /home/inumaki/Development/dotfiles/contracts/generated at pin 5eb01b8
```

`make gates-ci` (log `/tmp/opencode/req002-gates-ci-impl.txt`):

```text
test:            2250 passed, 3 skipped, 21 deselected
lint:            All checks passed!
contracts-check: 48 passed, 1 skipped
hermetic:        2250 passed, 3 skipped, 21 deselected
lock-check:      Resolved 25 packages
docs-check:      green
GATES_EXIT=0
```

`Verify:` rerun the focused command above, then `make gates-ci`; set
`CONTRACTS_REPO` to the sibling checkout for the positive pin check.

## Scope / constraints honored

- Runtime repo only; no other repo edited, no merge/push, no tag/release.
- No GUI, no host AGS quit/restart, no host process kill, no sudo: discovery/
  lanes use fake symlinked manifests, injected name/process oracles, a fake
  `ags` runner and a recording restart lane.
- BUG-006's confirmed-quit, intended-name verification, shared launch lock and
  exit-0 delegation classification are preserved and reused by the restart
  lane.
- Installed-spine only: discovery roots at `_resolve_install_spine()/config`;
  no composition checkout, no hardcoded app list.

## Closeout

`status: done` (2026-10-26). The implementation and its deterministic proofs
are on the ticket branch. Release/tag/pin work in `dotfiles-v3` remains a
separate chain (not requested or started here).
