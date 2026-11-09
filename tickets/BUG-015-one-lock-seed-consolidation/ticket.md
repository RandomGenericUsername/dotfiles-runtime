---
id: BUG-015
title: "First-run seed and icons regenerate still use a second .seed.lock instead of the one converge.lock"
kind: bug
status: done
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-015/
branch: ticket/BUG-015-one-lock-seed-consolidation
repro: "`rg -n 'seed\\\\.lock|FlockSeedMutex' src/runtime` and `ls -l ~/.local/state/dotfiles/.seed.lock`."
observed: "`src/runtime/cli/main.py` constructs `FlockSeedMutex(state_root / '.seed.lock')` for first-run seeding and for `icons regenerate`; `icons regenerate` holds it non-blocking while writing `current.json` and history independently of the v3 generation commit under `state/converge.lock`. The live state root still contains an empty `~/.local/state/dotfiles/.seed.lock`."
expected: "`state/converge.lock` is the only state-mutation lock created by any runtime command. First-run seeding runs as a sub-step of that one lock with a pass-through inner mutex (no nested flock, kernel release on crash). `icons regenerate` serializes through the same lock as wallpaper/skin/mode convergence, commits a generation, and appends its history line from the committed snapshot. A concurrent regenerate is surfaced as the existing non-blocking busy error instead of racing the convergence."
---

# First-run seed and icons regenerate still use a second `.seed.lock` instead of the one `converge.lock`

## Triage

- `kind: bug` — the v3 "one lock" contract (REQ-020) is violated by two remaining
  production callers of the legacy seed mutex, and `icons regenerate` mutates
  state/history without committing a generation.
- `lane: 2` — bounded composition/application fix plus regression tests; no
  OpenSpec change.
- `severity: high` — first-run seeding and the GUI live-toggle path can
  interleave with a daemon/CLI convergence on a different lock file, leaving
  `current.json`, history and the committed generation out of step.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` returned 0 on main
  (`25586c4`) before `create BUG-015 one-lock-seed-consolidation`.

## Evidence

- `Evidence:` `src/runtime/cli/main.py:328` constructs
  `FlockSeedMutex(state_root / ".seed.lock")` for first-run seeding.
- `Evidence:` `src/runtime/cli/main.py:1019` acquires
  `FlockSeedMutex(state_root / ".seed.lock").hold(blocking=False)` for
  `icons regenerate`; lines 1028-1031 save `current.json` and the following
  append writes history outside the v3 generation commit.
- `Evidence:` normal convergence uses `ConvergeLock` over
  `state/converge.lock` (`src/runtime/adapters/converge_lock.py:1-65`,
  `src/runtime/adapters/generation_store.py:75`), and
  `ReconcileDesktopStateUseCase` commits the generation, saves
  `current.json`, appends history and runs GC under that lock
  (`src/runtime/application/reconcile.py:256-520`).
- `Evidence:` the live state root has an empty
  `~/.local/state/dotfiles/.seed.lock`, while v3's frozen layout lists only
  `converge.lock` (`src/runtime/adapters/generation_store.py:1-28`).
- `Evidence:` `tests/unit/test_cli_icons.py:241-270` pins the current
  non-blocking busy contract by holding `.seed.lock`; that contract must move
  to `converge.lock`.

## Repro

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
rg -n 'seed\.lock|FlockSeedMutex' src/runtime
ls -l ~/.local/state/dotfiles/.seed.lock
```

## Observed

```text
src/runtime/cli/main.py:328:        mutex = FlockSeedMutex(state_root / ".seed.lock")
src/runtime/cli/main.py:1019:        with FlockSeedMutex(state_root / ".seed.lock").hold(blocking=False):
-rw------- 1 inumaki inumaki 0 ... ~/.local/state/dotfiles/.seed.lock
```

`icons regenerate` writes `current.json` and a history line while only the
legacy seed lock is held; it never commits a generation, so the history tail
can name an icons entry that `current/manifest.json` does not record.

## Expected

- `state/converge.lock` is the only lock file runtime commands create for
  state mutation. `.seed.lock` is not created anywhere.
- First-run seeding enters the same `converge.lock`, with the use case's inner
  mutex wired as a pass-through so there is no nested acquire/deadlock.
- `icons regenerate` participates in the one lock, commits a v3 generation,
  saves `current.json`, appends one `trigger="regenerate"` history line from
  the committed snapshot, and refreshes the AGS apps inside that scope.
- A second `icons regenerate`/convergence arriving while the lock is held is
  reported as the existing non-blocking busy error (GUI `wallpaper.state`
  `error`) rather than interleaving writes.
- Crash release is by kernel `flock`, so a killed seed/regenerate never wedges
  future commands.

## Fix notes

1. Add a non-blocking mode to `ConvergeLock` with a typed busy error; keep the
   default blocking for every ordinary convergence.
2. Run first-run seeding under `ConvergeLock` and inject
   `PassThroughSeedMutex`; delete `FlockSeedMutex` from production and shrink
   the old port to the pass-through used by already-held scopes.
3. Route `icons regenerate` through the reconcile commit path with a forced
   icons re-derive (history trigger `regenerate`, icons-layer details,
   AGS-only refresh) while the CLI holds the one lock; preserve the busy/error
   events.
4. Move the remaining production seed-mutex callers (regenerate pre-step,
   doctor repair, prune) onto `PassThroughSeedMutex` or `ConvergeLock`.
5. Regression tests: concurrent icons+wallpaper converge has one writer and
   tail == current == manifest; seed+concurrent command serializes with no
   deadlock; a killed lock holder releases; no `.seed.lock` is created; normal
   converge/refresh/GC/history/doctor stays green.

## Outcome

Implementation `4b35029` (fix + regression tests); release tag `v1.0.6`.

### Exact lock consolidation

- `ConvergeLock` (`state/converge.lock`) gained a typed non-blocking mode:
  `hold(blocking=False)` raises `ConvergeLockBusyError` instead of waiting.
  Ordinary convergence keeps the blocking default.
- First-run seeding (`_run_seed_if_needed`) now wraps `SeedCacheUseCase.run()`
  in `ConvergeLock(state_root).hold()` and injects
  `PassThroughSeedMutex`. No nested acquire, no `.seed.lock`; the kernel
  flock releases on process death.
- `icons regenerate` holds the same lock non-blocking (preserving the GUI
  busy/error contract), then runs `RegenerateIconsUseCase` with
  `acquire_lock=False`. The use case delegates to
  `ReconcileDesktopStateUseCase` with `force_icons=True`: one generation
  commit, `current.json` save, `trigger="regenerate"` history line with
  `details.layers.icons`, GC, and AGS refresh inside one scope. It never
  applies wallpaper pixels.
- `reconcile --regenerate-stale`'s pre-step and `doctor --repair` inject
  `PassThroughSeedMutex` because the engine/reconcile already owns
  `converge.lock`; real prune takes `ConvergeLock` directly. The
  `FlockSeedMutex` class and `state_root / ".seed.lock"` construction are
  deleted from production; only the pass-through inner mutex remains.
- History still uses its millisecond-scale `.history.lock`, which is not a
  convergence lock.

### Tests

New/updated coverage:

- `tests/unit/test_one_lock.py` — structural no-`FlockSeedMutex`/no
  `.seed.lock` path, non-blocking busy, killed-holder kernel release;
- `tests/unit/test_regenerate_icons.py` — concurrent icon runs serialize;
  concurrent icons regenerate + wallpaper reconcile has one writer and the
  final history tail == `current.json` == `current/manifest.json` on icons;
- `tests/unit/test_seed_cache.py` — two first-run seeds serialize on the one
  lock, exactly one save, no `.seed.lock`;
- `tests/unit/test_cli_icons.py` — the busy contract now pins the held
  `ConvergeLock` and the use case receives `acquire_lock=False`;
- existing crash/GC/generation/doctor/prune/reconcile suites run unchanged
  semantics under the consolidated lock.

Targeted run:

```sh
uv run pytest -q tests/unit/test_one_lock.py \
  tests/unit/test_regenerate_icons.py tests/unit/test_cli_icons.py \
  tests/unit/test_seed_cache.py tests/unit/test_reconcile.py \
  tests/unit/test_prune_cli.py tests/unit/test_wallpaper_visible_first.py \
  tests/unit/test_btop_theme_artifacts.py tests/unit/test_crash_recovery.py \
  tests/unit/test_generation_gc.py tests/unit/test_generations_v3.py \
  tests/unit/test_regenerate_stale.py tests/unit/test_doctor_repair.py \
  tests/unit/test_daemon_run.py
# 294 passed
```

Affected integration suites:

```sh
uv run pytest -q tests/integration/test_visible_first_swap_integration.py \
  tests/integration/test_crash_recovery_integration.py \
  tests/integration/test_reconcile_integration.py \
  tests/integration/test_seed_cache_integration.py \
  tests/integration/test_apply_wallpaper_integration.py \
  tests/integration/test_wallpaper_set_capstone_integration.py
# 32 passed
```

### Gates

```sh
make gates
# test: 2435 passed, 3 skipped, 11 deselected
# lint: All checks passed
# contracts-check: 58 passed, 1 skipped
# hermetic: 2435 passed, 3 skipped, 11 deselected
# lock-check: Resolved 25 packages
# docs-check: rc 0
```

`make gates-ci` and `make ci-gates-parity` are recorded on the release
commit.
