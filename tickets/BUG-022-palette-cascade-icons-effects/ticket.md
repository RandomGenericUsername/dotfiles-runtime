---
id: BUG-022
title: "Wallpaper set keeps stale icons/effects cache entries when a new palette or wallpaper changes their inputs"
kind: bug
status: done
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-022/
branch: ticket/BUG-022-palette-cascade-icons-effects
repro: "With a live state built for wallpaper A (palette 4836f90a..., icons e53912ea..., effects 1a3d8368...), run `dotfiles-runtime wallpaper set <wallpaper-B>` then read `$XDG_STATE_HOME/dotfiles/current.json`/`current/icons`: the palette moves to the new wallpaper, but icons stay at e53912ea... whose meta source_palette_hash is the old 0797efd7... instead of the new palette key 2f3bba51...."
observed: "`_ensure_entries` rebuilds icons only when the recorded icons cache dir is missing; a palette change leaves the old hash-named entry present, so it is treated as a cache hit and `current/icons` is literally the stale cache entry. Effects has the same shape: effects 1a3d8368... records source_wallpaper_hash fb1c5be6... while current wallpaper is d535c746...."
expected: "A wallpaper/mode/palette change cascades into icons (and a wallpaper change into effects): rebuild when the recorded layer source hash no longer matches the current palette/wallpaper, when the cache entry is absent, or when icons=None must heal. Same-input reconciles remain cache hits with no ITR/WEG call. A failed post-palette-change icon rebuild must not silently publish stale icon colors; it logs loudly and follows the existing graceful-degradation policy (drop the layer). Doctor reports source-hash divergence read-only and repair actually heals it."
---

# Wallpaper set keeps stale icons/effects cache entries

## Triage

- `kind: bug` — `ReconcileDesktopStateUseCase._ensure_entries` gates icons
  and effects on cache-directory existence, not on the recorded source
  identity. A palette change changes the icons cache key but leaves the old
  entry present, so the old entry is a legal (but wrong) cache hit.
- `lane: 2` — bounded reconcile/invalidate/doctor change plus regression
  tests; no OpenSpec change.
- `severity: high` — wallpaper set reports success while consumers read
  stale icon/effect colors; the bar reading `current/icons` is stale.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` returned 0 on main
  (`bf2f2c7`) before `create BUG-022 palette-cascade-icons-effects`.

## Evidence

- `Evidence:` `src/runtime/application/reconcile.py:797-812` (pre-fix)
  rebuilds effects only when the recorded effects cache dir is absent.
- `Evidence:` `src/runtime/application/reconcile.py:814-836` (pre-fix)
  rebuilds icons only when `force_icons` or the recorded icons cache dir is
  absent; the recorded `source_palette_hash` is never compared.
- `Evidence:` `src/runtime/application/derive.py:537-590` folds the palette
  hash into the icons entry key
  (`icons = sha256(palette_hash || templates_hash || mappings_hash)`), so a
  palette change implies a different icons directory.
- `Evidence:` live identities reported for this ticket: palette
  `4836f90a...`; icons `e53912ea...` with `source_palette_hash`
  `0797efd7...`; expected icons `2f3bba51...` absent from
  `cache/icons/`; effects `1a3d8368...` with source wallpaper
  `fb1c5be6...` while the current wallpaper is `d535c746...`.
- `Evidence:` ICME previews the live palette, not the icons cache entry, so
  its correct preview does not disprove the stale icons layer.

## Repro

```bash
# Check the recorded lineage of the live layers.
jq -r '.palette.hash, .icons.hash, .effects.hash' \
  "${XDG_STATE_HOME:-$HOME/.local/state}/dotfiles/current.json"
jq -r '.source_palette_hash' \
  "${XDG_STATE_HOME:-$HOME/.local/state}/dotfiles/cache/icons/$(jq -r '.icons.hash' "${XDG_STATE_HOME:-$HOME/.local/state}/dotfiles/current.json")/meta.json"
```

## Observed

The recorded icons entry is the old palette's entry and the expected new
icons cache directory does not exist. `current/icons` resolves into the old
cache entry, so the bar reads stale icon colors after a successful wallpaper
set.

## Expected

- Reconcile rebuilds icons when the palette changed (source mismatch), when
  the expected icons key is absent, or when the input-derived key differs
  (palette/templates/mappings).
- Reconcile rebuilds effects when the wallpaper changed (source mismatch) or
  the recorded cache entry is absent.
- Same-input reconciles stay cache hits and invoke no ITR/WEG tool.
- A failed icon rebuild after a palette change logs loudly and drops the
  stale layer (no false success with old colors).
- Doctor reports source-hash divergence read-only with a repair hint; repair
  quarantines the stale entry so the next reconcile rebuilds it.

## Fix notes

- Make the icons trigger palette-aware (and input-key-aware) instead of
  existence-only; apply the same structural cascade to effects.
- Keep the reproducibility assertion only when the recorded inputs did not
  change.
- Add the doctor consistency leg and regression tests (unit + production
  `_run_wallpaper_set` path). No composition/assets/ags-bar/live-state edit
  and no apply in this slice.

## Outcome (2026-11-12)

**Worktree:** `.worktrees/BUG-022/`
**Branch:** `ticket/BUG-022-palette-cascade-icons-effects`
**Commits:** `cc237d4` (ticket + INDEX plan), `2d216b5` (reconcile/doctor
cascade + regression tests), this release commit (version metadata/CHANGELOG/
README + ticket close).
**Tag:** `v1.5.3` (annotated) after green gates.

### Delivered

| Path | What changed |
| --- | --- |
| `src/runtime/application/derive.py` | internal `expected_effects_hash` / `expected_icons_hash` probes from the current spine inputs |
| `src/runtime/application/reconcile.py` | palette source check; icons rebuild on source/key/cache-null divergence; effects rebuild on source/key/cache/null; reproducibility assertion only when inputs are unchanged; failed post-change icon rebuild logs and drops the stale layer |
| `src/runtime/application/doctor.py` | read-only `source_palette_hash`/`source_wallpaper_hash` lineage leg from cache `meta.json`, with repair hint |
| `tests/unit/test_reconcile.py` | wallpaper A→B palette/icons/effects cascade, same-wallpaper zero-ITR cache hit, null-icons heal, template/mapping key changes, ITR failure policy |
| `tests/unit/test_doctor_check.py` | icons and effects lineage divergence reported read-only |
| `tests/unit/test_doctor_repair.py` | repair quarantines stale entries, rebuilds them, and the post-repair check is clean |
| `tests/integration/test_wallpaper_set_capstone_integration.py` | production `_run_wallpaper_set` path (not the old apply→reconcile helper) proves the icons cascade, manifest intent and `current/icons` target |
| `pyproject.toml` / `src/runtime/__init__.py` / `uv.lock` / `CHANGELOG.md` / `README.md` | `1.5.3` release metadata and notes |

### Failure policy

- A failed icon rebuild after a palette/key change logs at error level and
  sets `icons=None`; the committed generation omits `current/icons`, so the
  bar cannot read the old palette's icon colors. This matches the existing
  graceful-degradation contract for optional derived layers.
- A failed effects rebuild after a wallpaper/catalog change follows the same
  policy (log and drop).
- Unchanged-input cache misses keep the reproducibility assertion; changed
  inputs (palette, wallpaper, templates, mappings, catalog) are legitimate
  rebuilds and do not assert the old key.

### Targeted evidence

```sh
uv run pytest -q tests/unit/test_reconcile.py \
  tests/unit/test_doctor_check.py tests/unit/test_doctor_repair.py \
  tests/integration/test_wallpaper_set_capstone_integration.py
# 161 passed
```

### Gates

```sh
make gates
# test: 2607 passed, 3 skipped, 11 deselected
# lint: All checks passed
# contracts-check: 58 passed, 1 skipped
# hermetic: 2607 passed, 3 skipped, 11 deselected
# lock-check: Resolved 25 packages
# docs-check: rc 0

make gates-ci
# same six gates: 2607 passed, 3 skipped, 11 deselected

make ci-gates-parity
# PASS gates=['contracts-check','docs-check','hermetic','lint','lock-check','test']
```

`uv run mypy src` is not a repo gate and reports the pre-existing 127-error
baseline; none of the reported errors are on the new cascade/probe/doctor
lines.

### Downstream pin needed

- **Composition pin:** `dotfiles-runtime` must move to `v1.5.3` so the next
  converge rebuilds stale icons/effects on hosts whose current generation was
  produced before this fix.
- No composition, assets, AGS-bar, provisioning, contract or live-state edit
  was made, and no `apply` was run.
