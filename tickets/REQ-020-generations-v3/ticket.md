---
id: REQ-020
title: "Generations v3: one committed generation and one converge(intent)"
kind: requirement
status: done
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-020/
branch: ticket/REQ-020-generations-v3
---

# REQ-020 — Generations v3: one committed generation and one converge(intent)

## Requirement

Implement the frozen v3 generation interface in `dotfiles-runtime` only:

1. **Generation model.** `state/generations/<genHash>/` contains
   `manifest.json` (contracts v0.8.0
   `schemas/generation-manifest.schema.json`), `palette/`, `appearance/<configDir>.css`
   plus `appearance/tokens.json`, `icons/`, `effects/` and `wallpaper`. Stage in a
   sibling `generations/.staging-*`, fsync files and directories, then publish with
   exactly one atomic `state/current -> generations/<genHash>` rename. Every
   consumer reads through `current/`. Layer entries remain content-addressed,
   referenced/reused; the manifest records layer hashes plus skin/mode/source.
2. **One `converge(intent)`.** Daemon reaction to `desired.json`, CLI reconcile,
   wallpaper set and skin/mode changes call the same function. The reconcile vs
   wallpaper-set divergence is removed; `mode` regenerates the palette in every
   path (the live defect: reconcile ignored `mode=light` while wallpaper set
   honoured it).
3. **Serialization.** The daemon owns a converge job queue over the existing
   `org.dotfiles.Events` job surface (`BeginJob`/`JobStarted`/`JobFinished`),
   coalescing identical intents and superseding queued (not running) intents.
   The CLI preflights the bus name once, subscribes to `JobFinished` before
   submitting, submits, and waits for its job/genHash without polling state
   files. With no daemon it runs the same function under the same exclusive
   kernel `flock` (blocking, no sleep loop). The fallback never runs concurrently
   with a possibly-live job. One lock scope covers intent → derive → stage →
   commit → apply/ledger; no nested seed/appearance locks.
4. **Generation-bound refresh.** Send `ags request appearance-refresh <genHash> -i
   <instance>`; accept only an ack whose `genHash` equals the request; record
   `applied` only on an exact match; deferred/retry on the next trigger; never
   treat `{"ok":true}` or a mismatch as applied. Enforce the app's declared
   `live|restart` lane exactly; unit-managed restarts use `systemctl --user
   restart`; a failed live refresh never silently becomes a restart.
5. **No polling as correctness.** AGS waits use D-Bus `NameOwnerChanged` (Gio bus
   watch) and systemd waits use `JobRemoved` / `systemctl --wait`; timeouts are
   abort/report bounds only. The lock retry sleep loop is removed.
6. **Locality and immutability.** Never write under `<install>/` or
   `apps/<tag>`; stop creating/repairing `config/<app>/appearance.css` and v1/v2
   skin pointers; prune them. App-visible artifacts are state-only. Keep legacy
   consumer links (GTK/rofi/btop) pointing at `current/palette/...`; do not
   create tag-target links.
7. **Compatibility and recovery.** `current.json` becomes a derived compatibility
   view (dual-read shim) while `current/manifest.json` is authoritative; define
   crash recovery before/after the commit rename; GC by generation references
   plus `gc-pins.json`, never deleting an acknowledged generation.
8. **Tests.** Crash injection before/after commit; concurrent daemon+CLI; intent
   coalescing; mode-only reconcile regenerates the palette; generation-bound ack
   mismatch is refused; no writes under app tags; event-driven waits (no polling
   loops); GC pins; doctor/inspect show desired vs committed vs applied.
9. **Docs/CHANGELOG; tag `v1.0.0`; fast-forward merge to main; push main + tag.**
   Do not run `apply`.

## Acceptance

- [x] `state/generations/<genHash>/` matches the frozen layout, `manifest.json`
      validates against the v0.8.0 schema, and `current` is one symlink committed
      by a single `rename`.
- [x] Layer entries are content-addressed and referenced, not copied; the
      manifest records layer hashes, skin and mode.
- [x] `converge(intent)` is the only mutation path for daemon, reconcile,
      wallpaper set and skin/mode changes.
- [x] A `mode`-only change through reconcile regenerates the palette and the
      committed generation reports the new mode.
- [x] Daemon queue coalesces identical intents, supersedes queued ones, and never
      cancels a running job; CLI subscribes before submitting and waits on
      `JobFinished`, not polling.
- [x] No daemon: the same function runs under the one blocking `converge.lock`;
      the old seed/appearance locks and sleep loops are gone.
- [x] Live refresh is generation-bound; stale/boolean acks are refused and
      retried; the declared lane is enforced and a live failure never restarts.
- [x] AGS registration uses `NameOwnerChanged`; systemd waits use
      `JobRemoved`/`systemctl --wait`; timeouts only abort/report.
- [x] Runtime writes only under `state/`; no `config/<app>/appearance.css`,
      v1/v2 skin pointer, or app-tag write remains; legacy consumer links point
      through `current/palette/...`.
- [x] `current.json` is a derived compatibility view; crash recovery covers
      before/after the commit rename; GC retains active/newest/pinned/ledger and
      never deletes an acknowledged generation.
- [x] Tests cover the nine listed scenarios.
- [x] README/CHANGELOG/ARCHITECTURE describe v3; version is `1.0.0`; `make
      gates` and `make gates-ci` pass; tag `v1.0.0` is created after the green
      commit and merged fast-forward to `main`; main and tag are pushed.

## Evidence

Frozen design: `dotfiles-v3/docs/107-generations-v3.md` §0 (decisions),
§2.1–§2.10 (layout, manifest, commit, crash matrix, converge, refresh, locality,
lanes, events, GC), §4 (acceptance A-1…A-10).

Frozen contracts at `contracts` v0.8.0:

- `generations.md` §1 (layout; `current` is one symlink), §2 (manifest and
  identity rules), §3 (commit transaction and daemon queue/fallback), §5
  (consumer read/watch), §6 (locality), §7 (lane policy), §9 (GC + pins), §10
  (event-driven rules), §11 (generation-bound refresh).
- `schemas/generation-manifest.schema.json` (draft-07, `additionalProperties:
  false`, `version: 3`, hashes, relative generation-local layer paths).
- `appearance-refresh.md` (request `appearance-refresh <genHash>`, response hash
  must equal the request, `{"ok":true}` invalid).

Baseline defects on `main` @ `231eab4` (v0.9.5):

- `src/runtime/adapters/seeder.py:660-807` repoints independent `current/`
  family links and writes `config/<configDir>/appearance.css`; `current` is a
  directory, not a generation pointer.
- `src/runtime/application/reconcile.py:238-326` derives outside the lock and
  publishes palette/appearance/consumer links as separate instants.
- `src/runtime/adapters/appearance_ledger.py:58-94` retries `flock` with
  `time.sleep(0.05)`; `src/runtime/adapters/ags_reloader.py:757-828` polls
  names/processes.
- `src/runtime/adapters/ags_app_refresh.py:151-218,592-599` accepts the v2
  `{"ok":true}` ack and sends `palette-refresh`.
- `src/runtime/cli/main.py` has separate `_run_reconcile`, `_run_wallpaper_set`,
  `_run_reactive_converge` compositions.

Verify baseline:

```sh
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
rg -n "def repoint_current_symlinks|def publish_appearance|hold_operation_lock|palette-refresh|def _run_reconcile|def _run_wallpaper_set" \
  src/runtime/adapters/seeder.py src/runtime/adapters/appearance_ledger.py \
  src/runtime/adapters/ags_app_refresh.py src/runtime/cli/main.py
```

## Implementation map

- `src/runtime/adapters/generation_store.py` — staging, fsync, manifest,
  publish rename, `current` rename, verification, derived `current.json`, GC and
  pins.
- `src/runtime/application/converge.py` — `ConvergeEngine.converge(intent)`;
  the one lock scope and the only mutation function.
- `src/runtime/adapters/dbus_converge.py` — one-shot bus preflight,
  subscribe-before-submit `JobFinished` wait, typed fallback decision.
- `src/runtime/adapters/daemon_converge_queue.py` — daemon queue with
  coalescing/supersede over the existing job surface.
- `src/runtime/adapters/ags_app_refresh.py` — v3 `appearance-refresh <genHash>`
  request and ack validation.
- `src/runtime/adapters/ags_reloader.py` — event-driven name/systemd waits.
- `src/runtime/adapters/seeder.py` — no tag/spine writes; v1/v2 pointer pruning.
- `src/runtime/application/reconcile.py`, `src/runtime/application/apply_wallpaper.py`,
  `src/runtime/cli/main.py` — route through `converge(intent)`.
- `tests/unit/test_generations_v3.py` and the updated existing suites — the
  nine required scenarios.
- `README.md`, `CHANGELOG.md`, `docs/ARCHITECTURE.md`, `pyproject.toml` —
  v3 documentation and `1.0.0` release metadata.

## Final evidence

Commands run on `ticket/REQ-020-generations-v3`:

```sh
make gates
# test 2372 passed, 3 skipped, 10 deselected
# lint All checks passed
# contracts-check 58 passed, 1 skipped
# hermetic 2372 passed, 3 skipped, 10 deselected
# lock-check Resolved 25 packages

make gates-ci
# same six gates; exit 0

CONTRACTS_REPO=/home/inumaki/Development/dotfiles/contracts python3 scripts/contract-pin-check.py
# PASS — vendored bindings match contracts/generated at pin d86c19c…

uv run pytest -q tests/unit/test_generations_v3.py
# 10 passed
```

Focused v3 proofs:

- `tests/unit/test_generations_v3.py` — layout + one pointer, queue coalescing
  and supersede, exclusive converge lock, generation-bound stale/boolean ack
  refusal, no app-tag writes, no sleep polling in the converge core,
  desired/committed/applied inspection, GC acknowledged retention.
- `tests/unit/test_crash_recovery.py` — injected crash before the generation
  rename, after it, after the pointer rename, ledger mismatch, GC pins.
- `tests/unit/test_reconcile.py::test_mode_change_regenerates_the_palette_entry`
  — mode-only reconcile commits a light generation.
- `tests/unit/test_ags_app_refresh.py` — v3 `appearance-refresh` transport and
  lane policy.
- `tests/unit/test_skins.py`, `test_seed_cache.py`, `test_doctor_repair.py`,
  `test_doctor_check.py`, `test_appearance.py` — v3 locality, generation
  identity and current.json compatibility behaviour.

## Deviations

- The installed AGS restart adapter keeps its injected `instance_lister`
  oracle for headless tests; production (`instance_lister=None`) uses the
  D-Bus `NameOwnerChanged` watcher. The launch lock is now a single
  non-blocking flock attempt (fail-fast) instead of a bounded retry loop.
- `current.json` remains the v2 schema as the derived compatibility view;
  `current/manifest.json` is authoritative and every runtime decision reads it.
- The legacy `AppearanceGenerator` v2 cache entry is still composed as an
  input to the generation (its sheets are copied into the immutable
  generation); no v2 pointer is published.
