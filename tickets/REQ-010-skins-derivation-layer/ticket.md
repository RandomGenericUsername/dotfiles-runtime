---
id: REQ-010
title: "Skins derivation layer: palette-independent per-app CSS + token map"
kind: requirement
status: done
lane: 2
links:
  openspec: skins-derivation-layer
  bmad: none
worktree: .worktrees/REQ-010/
branch: ticket/REQ-010-skins-derivation-layer
---

# REQ-010 — Skins derivation layer

## Requirement

Add a palette-independent **skin** derivation layer to `dotfiles-runtime`. A skin is a
palette/ink mapping, paint and rhythm description only: it carries **no** structure or
variant tokens. The runtime selects a skin from `desired.json` (`"skin": "<name>"`,
default `glass`) and expands the pinned assets template set plus the chosen manifest into
per-app CSS.

The layer mirrors the existing palette/effects/icons layers:

- manifest parsing/validation accepts JSON or TOML;
- template expansion is deterministic `{{SKIN_<TOKEN>}}` substitution (no new runtime
  dependency);
- the cache key is `sha256(templates_hash ‖ manifest_hash)`, so a wallpaper or palette
  change never re-expands a skin;
- each entry is `cache/skins/<sha256>/` with `meta.json` (kind `skin`);
- `current/skins/<app>.css` and `current/skin.json` are atomically repointed like colors;
- AGS gets stable consumer pointers `{install}/config/ags/skin.css` →
  `current/skins/ags.css` and `{install}/config/ags/skin.json` → `current/skin.json`;
- the appearance hash includes the skin entry hash, so live apps refresh through the
  existing `palette-refresh` lane while the restart-only bar stays restart-only;
- failure is fail-closed per skin: a missing/invalid manifest or template keeps the
  previous skin, logs a structured error, and never touches app CSS.

## Acceptance

- [x] `desired.json` accepts an optional `skin` string (default `glass`); malformed values
      fail loud with the path and field.
- [x] `current.json` carries an optional `skin` projection (`hash`, `name`, `generated_at`)
      validated by the embedded/canonical schema pair.
- [x] `SkinExpander` parses JSON and TOML manifests, validates the frozen v1 shape, substitutes
      `{{SKIN_*}}` tokens deterministically, and writes `<app>.css` plus `skin.json`.
- [x] The cache key is exactly `sha256(templates_hash ‖ manifest_hash)`; wallpaper/palette
      changes are cache hits and template/manifest changes are misses.
- [x] A skin cache entry is `cache/skins/<hash>/` with `meta.json` containing
      `hash_algorithm: "sha256"`, `kind: "skin"`, `entry_hash`, `name`, input hashes and
      `artifact_hashes`.
- [x] `reconcile`/`seed`/`wallpaper set` derive the selected skin; missing skin assets keep
      the previous skin, emit a structured log and leave app CSS/pointers untouched.
- [x] `current/skins/<app>.css` and `current/skin.json` are atomic symlinks; AGS consumer
      pointers are in the static spec table and skip safely when absent.
- [x] `appearance_hash` changes when the skin hash changes; `appearance_hash_for_state`
      includes the skin.
- [x] `inspect status` prints the resolved skin and its token map; doctor covers the skin
      cache entry, current links and AGS consumer pointers.
- [x] `inspect cache list --verify` and prune include the `skins` layer.
- [x] Tests cover cache-hit stability, cache misses, invalid-manifest fallback, atomic
      repoint, appearance-hash sensitivity and the `glass` default.
- [x] `make gates` (test lint contracts-check hermetic lock-check docs-check) and
      `make gates-ci` pass; ARCHITECTURE/README/CHANGELOG updated.
- [x] Release tag is the next annotated minor (`v0.7.0`); no existing tag is moved.

## Frozen spec inputs

The execution contract is the owner-supplied **FROZEN SKIN SPEC v1** (GTK 4.24 probes):
palette/ink mapping + paint + rhythm only; no structure/variant tokens; default `glass`;
assets v1 manifests `glass`, `flat`, `paper`.

## Evidence

Baseline (before this change):

```sh
rg -n "skin" src/runtime tests --glob '!**/__pycache__/**'
# no matches: no skin layer, no desired/current field, no consumer pointer
```

Cache/layer contract that this ticket mirrors:

```sh
sed -n '1,40p' src/runtime/adapters/hashing.py
sed -n '48,62p' src/runtime/adapters/cache.py
sed -n '48,57p' src/runtime/adapters/consumer_path_spec.py
```

Verify after implementation:

```sh
uv run pytest -q tests/unit/test_skins.py tests/unit/test_skin_expander.py
uv run pytest -q tests/unit/test_desired_state.py tests/unit/test_json_state_repository.py
make gates
```

## Implementation map

- `src/runtime/domain/models.py` — `SkinEntry`, `DEFAULT_SKIN`, `SkinUnavailableError`.
- `src/runtime/ports/skin_expander.py` — expansion port.
- `src/runtime/adapters/skin_expander.py` — manifest parse/validate + expansion.
- `src/runtime/adapters/hashing.py` — `skin_entry_hash`, appearance hash input.
- `src/runtime/adapters/cache.py` — `skins` layer.
- `src/runtime/adapters/seeder.py` — skin meta/loader, current + consumer repoint.
- `src/runtime/adapters/consumer_path_spec.py` — AGS skin pointers.
- `src/runtime/application/derive.py` — skin asset discovery + `ensure_skin`.
- `src/runtime/application/skins.py` — resolved asset/error helpers.
- `src/runtime/application/{reconcile,seed_cache,apply_wallpaper,swap_visible}.py` —
  derive/preserve/repoint integration.
- `src/runtime/application/{inspect,doctor,prune,actual_state}.py` — projection/health/prune.
- `src/runtime/adapters/{json_state_repository,desired_state_reader,appearance_ledger}.py`.
- `schemas/{current,meta,history}` embedded + canonical copies, `desired` reader docstring.
- `src/runtime/cli/main.py` — desired-skin resolution, wiring, inspect output.


## Implementation evidence

Executed on the ticket worktree (2026-11-05):

```sh
uv run pytest -q tests/unit/test_skin_expander.py tests/unit/test_skins.py
# 18 passed
```

```sh
make gates
# test       2228 passed, 3 skipped, 10 deselected
# lint       All checks passed
# contracts  48 passed, 1 skipped
# hermetic   2228 passed, 3 skipped, 10 deselected
# lock-check Resolved 25 packages (no changes)
# docs-check exit 0
```

Focused integration proof (fakes, no live desktop):

```sh
uv run pytest -q tests/integration/test_wallpaper_set_capstone_integration.py \
  tests/integration/test_inspect_cache_list_integration.py \
  tests/integration/test_reconcile_integration.py
# 18 passed
```

Evidence map (verify with `rg`/`sed`):

- `src/runtime/adapters/skin_expander.py` — manifest JSON/TOML parse + v1
  validation + deterministic `{{SKIN_*}}` expansion.
- `src/runtime/adapters/hashing.py:skin_entry_hash` — `sha256(templates ‖ manifest)`.
- `src/runtime/adapters/hashing.py:appearance_hash` — includes `skins`.
- `src/runtime/application/derive.py:ensure_skin` — staging cache entry + fail-closed error.
- `src/runtime/application/reconcile.py:_ensure_entries` — derive/keep-previous skin + repoint.
- `src/runtime/adapters/consumer_path_spec.py` — AGS `skin.css`/`skin.json` pointers.
- `src/runtime/adapters/schemas/{current,meta,history}.schema.json` + `contracts/schemas/` — byte-identical.
- `tests/unit/test_skins.py` — cache hit/miss, invalid fallback, atomic repoint, appearance, glass default.
