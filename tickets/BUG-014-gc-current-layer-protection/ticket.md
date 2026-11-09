---
id: BUG-014
title: "Converge GC deletes the committed current generation's palette/wallpaper/appearance cache layers"
kind: bug
status: done
lane: 2
severity: critical
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-014/
branch: ticket/BUG-014-gc-current-layer-protection
repro: "`uv run pytest -q tests/unit/test_generation_gc.py -k 'live_values or current_refs'` (hermetic: seed the live palette/wallpaper/effects/icons refs, commit the generation, run `GenerationStore.gc(keep=0, grace_seconds=0)`)."
observed: "The live daemon journal shows `reconcile: committed generation 43d62e17e2da` immediately followed by `reconcile: gc swept {'generations': [], 'cache': ['wallpapers/fb1c5be6...', 'palettes/0797efd7...', 'palettes/35f53fb9...', 'appearance/09ea6912...', 'appearance/c93da79f...', 'appearance/da5db5d9...'], 'staging': []}`. The committed/current generation references palette `0797efd7...` and wallpaper `fb1c5be6...`; after GC `current/palette/colors.*` and `current/wallpaper/wallpaper.png` dangle. `doctor` reports `dangling: 10, diverged: 1, missing: 2` and the engine `state_links` assert fails with \"a provisioning-owned stable consumer link does not resolve under current/palette\". A hermetic reproduction on v1.0.4 sweeps `wallpapers/<current>`, `palettes/<current>` and `appearance/<current>` while `effects`/`icons` survive."
expected: "GC protects every cache layer referenced by the post-commit `state/current` manifest (wallpaper, palette, effects, icons and the composed appearance entry), plus ledger-referenced layers, the newest entry per layer and explicit generation pins. The current generation's layer links always resolve after GC; only unreferenced, older-than-grace entries are removed; a subsequent converge/restart leaves `doctor` clean."
---

# Converge GC deletes the committed current generation's palette/wallpaper/appearance cache layers

## Triage

- `kind: bug` — the committed generation is self-consistent, but GC's
  cache-protection set is built from the wrong manifest layer names.
- `lane: 2` — bounded adapter/use-case fix plus regression tests; no
  OpenSpec change.
- `severity: critical` — the live desktop's `current/palette` pointer no
  longer resolves; a restart of a palette consumer would fail, and the
  engine's provisioning-owned `state_links` invariant is broken.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` returned 0 on main
  (`f15d576`) before `create BUG-014 gc-current-layer-protection`.

## Evidence

- `Evidence:` live journal from a provisioning `apply` on v1.0.4:
  `reconcile: committed generation 43d62e17e2da` then
  `reconcile: gc swept {'generations': [], 'cache': ['wallpapers/fb1c5be6...',
  'palettes/0797efd7...', 'palettes/35f53fb9...', 'appearance/09ea6912...',
  'appearance/c93da79f...', 'appearance/da5db5d9...'], 'staging': []}`.
- `Evidence:` `src/runtime/adapters/generation_store.py:376-384` writes
  manifest layer keys as `wallpaper`, `palette`, `effects`, `icons`, while
  `generation_store.py:774` sweeps cache directory names `wallpapers`,
  `palettes`, `effects`, `icons`, `appearance` and looks the manifest key up
  by the cache name at line 778. `wallpapers`, `palettes` and `appearance`
  therefore get an empty keep-set; only the identically named `effects` and
  `icons` survive, exactly matching the journal.
- `Evidence:` the appearance cache entry is a composition input and has no
  manifest layer key at all: `generation_store.py:177` hashes it into the
  generation identity as `appearance.artifactHash`, but the frozen v3
  manifest records only `tokensPath`/`targets` (`generation_store.py:502-506`).
  GC must recompute `appearance_generation_hash(palette, skin, mode)` from the
  manifest.
- `Evidence:` `src/runtime/application/reconcile.py:553-572` runs GC after the
  `with self._mutex.hold(...)` block, i.e. after the commit/current.json/history
  scope, and passes the ledger's appearance hash through
  `ledger_generations` (`reconcile.py:558-565`); that value is an appearance
  layer, not a generation.
- `Evidence:` hermetic v1.0.4 reproduction (temp state built from
  `tests/unit/test_doctor_check._seed_state`, then
  `GenerationStore.gc(keep=0, grace_seconds=0, acknowledged_generations=[current])`):

  ```text
  swept {'generations': [], 'cache': ['wallpapers/dddd...',
    'palettes/aaaa...', 'appearance/4c83...'], 'staging': []}
  after ['cache/effects/bbbb...', 'cache/icons/cccc...']
  palette/colors.yaml DANGLING
  wallpaper/wallpaper.png DANGLING
  effects exists
  icons exists
  ```

## Repro

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
uv run pytest -q tests/unit/test_generation_gc.py -k 'live_values or current_refs'
```

The hermetic test seeds the live palette `0797efd7...`, wallpaper
`fb1c5be6...`, effects `1a3d8368...`, icons `e53912ea...` and the composed
appearance entry, commits the generation, then runs GC with `keep=0` and
`grace_seconds=0`.

## Observed

GC removes `wallpapers/<current>`, `palettes/<current>` and
`appearance/<current>`; `current/palette/colors.*` and
`current/wallpaper/wallpaper.png` become dangling. Effects/icons survive only
because their manifest key equals their cache directory name.

## Expected

- The GC protection set is computed from the post-commit current generation
  under the same convergence lock: every `manifest.layers` entry mapped to its
  cache directory, plus the composed appearance hash, plus ledger-referenced
  layers, newest-per-layer and explicit pins.
- Commit → protect current → prune is one locked ordering; no pre-commit
  snapshot enters GC.
- Unreferenced entries older than grace still prune.
- After GC, every current generation link resolves and `doctor` is clean;
  restarting/converging does not repair damage because none was caused.

## Fix notes

1. Map manifest layer keys to cache directories (`wallpaper`→`wallpapers`,
   `palette`→`palettes`, `effects`→`effects`, `icons`→`icons`) and include
   `manifest.intent` refs. Recompute the current generation's composed
   appearance hash from `layers.palette.entryHash`, `skin.entryHash`, `mode`
   and protect `cache/appearance/<hash>`.
2. Add explicit `current_generation` and `ledger_layers` inputs to
   `GenerationStore.gc`; protect `ledger_generations` for compatibility and
   stop passing an appearance hash as a generation. Protect the newest entry
   per swept layer and fail the cache sweep closed when the current manifest
   cannot be read.
3. Move the GC call inside the reconcile commit scope (after
   `append_history`, before releasing the mutation/converge lock) and pass the
   just-committed generation explicitly. This makes the protected set
   post-commit, never a pre-commit snapshot.
4. Regression tests: live-value current-ref protection for all five layers,
   older unreferenced pruning, ledger appearance refs, pinned generation
   layers, newest-per-layer, GC under the converge lock after commit, and
   doctor clean after a converge following GC.

## Outcome

Implementation `c2c3d77` (fix + regression tests); release tag `v1.0.5`.

### Exact GC protection fix

`GenerationStore.gc` now derives cache protection from every retained
generation manifest (including the explicit post-commit
`current_generation`):

1. **Layer-name mapping.** Singular manifest keys are mapped to plural cache
   directories: `wallpaper → wallpapers`, `palette → palettes`,
   `effects → effects`, `icons → icons`. Both `manifest.layers[*].entryHash`
   and `manifest.intent[*]` are read.
2. **Composed appearance.** The current generation's appearance cache entry
   is recomputed as `appearance_generation_hash(layers.palette.entryHash,
   skin.entryHash, mode)` and added to the `appearance` keep-set. The frozen
   v3 manifest deliberately has no appearance layer key, so this recompute is
   the only way to protect that composition input.
3. **Ledger refs.** `ledger_generations` still protects the v3 ledger's
   generation hash; the new `ledger_layers` mapping protects a legacy v2
   composed-appearance hash when present. Reconcile passes both.
4. **Newest per layer and pins.** The newest entry in each swept layer is
   protected (race-created derives), and pinned generations keep their
   manifest-referenced layers.
5. **Fail closed.** If any current generation (pointer or explicit) has no
   readable manifest, generation/staging pruning still runs but the cache
   sweep is skipped.
6. **Ordering.** The GC call moved inside the reconcile commit scope,
   immediately after commit → `current.json` save → history append and before
   releasing `converge.lock`, with `current_generation` passed explicitly.
   The protected set is therefore post-commit, never a pre-commit snapshot.

### Regression tests

`tests/unit/test_generation_gc.py` (8 tests):

- live palette `0797efd7…`, wallpaper `fb1c5be6…`, effects `1a3d8368…`, icons
  `e53912ea…` and composed appearance survive GC; all `current/*` links
  resolve; older unreferenced entries in all five layers prune; doctor clean;
- pinned generation layers survive `keep=0, grace=0`;
- ledger-referenced generation and legacy appearance layer survive;
- newest-per-layer survives while older unreferenced entries prune;
- a corrupt current manifest fails the cache sweep closed;
- commit → GC order, `current_generation == current_hash`, GC runs while
  `converge.lock` is held (a second converge blocks);
- reconcile wires the previous ledger appearance ref into GC.

```sh
uv run pytest -q tests/unit/test_generation_gc.py \
  tests/unit/test_generations_v3.py tests/unit/test_crash_recovery.py \
  tests/unit/test_reconcile.py tests/unit/test_doctor_check.py \
  tests/unit/test_regenerate_stale.py tests/unit/test_reactive_converge.py
# 160 passed
```

### Gates

```sh
make gates
# test: 2431 passed, 3 skipped, 11 deselected
# lint: All checks passed
# contracts-check: 58 passed, 1 skipped
# hermetic: 2431 passed, 3 skipped, 11 deselected
# lock-check: Resolved 25 packages
# docs-check: rc 0
```

### Live read-only evidence (no apply)

`uv run dotfiles-runtime doctor --format json` from the fixed worktree (no
apply, no repair) reproduces the unrepaired live state on generation
`43d62e17e2da…`:

```text
clean=false
ok=14 dangling=10 missing=2 diverged=1
missing: cache/wallpapers/fb1c5be6…, cache/palettes/0797efd7…
dangling: current/wallpaper*.png, current/colors.* (10 links)
```

`state/current -> generations/43d62e17e2da…` still resolves, but its palette
and wallpaper cache entries are absent. v1.0.5 prevents new damage; the live
cache is repaired after installing the tag, as agreed (no apply here).
