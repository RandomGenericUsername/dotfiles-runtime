---
id: REQ-014
title: "Publish one appearance generation and one atomically repointed pointer"
kind: requirement
status: done
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-014/
branch: ticket/REQ-014-appearance-plane
---

# REQ-014 — Publish one appearance generation and one atomically repointed pointer

## Requirement

The runtime currently publishes palette and skin as two independent artifact
sets, two `current/` paths and two consumer pointer families. Replace that
split publication with **one composed appearance generation**, while keeping
the palette and skin caches independent so a wallpaper change reuses the skin
expansion and a skin change reuses the palette.

The generation lives at `cache/appearance/<appearanceHash>/` and contains:

- `colors.yaml` — the selected palette entry's `colors.yaml` (byte copy);
- `tokens.json` — the mode-resolved v2 token document, validated against
  `appearance-json.schema.json`;
- `appearance.json` — `version: 2`, `appearanceHash`, `paletteHash`,
  `skinHash`, `generationHash`, `skin`, `mode`, `tokenRef`, validated against
  `appearance-artifact.schema.json`;
- one `<configDir>.css` per skin target, composed in order from the palette
  legacy CSS (`colors.gtk.css` bytes), the rendered palette role block
  (`@define-color <role> <hex>;`) and the expanded skin CSS.

Publication:

- `current/appearance` is ONE symlink, atomically repointed to the generation
  directory (single hop: write a temp symlink then `os.replace`).
- `config/<configDir>/appearance.css` → `current/appearance/<configDir>.css`
  is a stable convenience link, created only for existing app config dirs.
- v1 `config/<configDir>/skin.{css,json}` and `current/skins/`,
  `current/skin.json` pointers remain during the migration window so v1 apps
  keep working; their removal is a later ticket.

Identity and mode:

- `appearanceHash = sha256("appearance.v2\n" + paletteHash + "\n" + skinHash +
  "\n" + mode)`; palette/skin caches are never mutated by this composition.
- Mode is derived from the generated palette (`colors.yaml`/producer output).
  A `mode`/`scheme`/`appearance`/`variant` scalar or `is_dark`/`dark` boolean
  selects the mode; if the palette carries no mode field, generation defaults
  to `dark` and the fallback is documented. Mode is part of the identity.
- A v2 skin manifest's `mode.dark`/`mode.light` override replaces its
  same-shape base group before `tokens.json` and CSS are written. A v1 skin
  manifest is translated through the RFC §5.2 compatibility mapping for this
  migration window (the v1 skin cache artifacts stay authoritative).
- The appearance ledger/refresh uses the appearance hash; the existing
  `palette-refresh` transport is unchanged (whole-appearance semantics).

Reconcile/seed publish the appearance in the same atomic flow, prune stale
appearance entries (keep-N + active + grace) and stale runtime-owned
convenience links. `inspect status` and `doctor` report the appearance
hash/skin/mode/pointer and watch health; `doctor` detects the v1/v2 skew
(`current/appearance` present while config links point at v1 skin).

## Acceptance

- [x] `cache/appearance/<appearanceHash>/` holds `colors.yaml`,
      `tokens.json`, `appearance.json` and one `<configDir>.css` per target.
- [x] `tokens.json` validates against `appearance-json.schema.json`;
      `appearance.json` validates against `appearance-artifact.schema.json`.
- [x] `current/appearance` is a single-hop symlink repointed atomically.
- [x] `config/<configDir>/appearance.css` convenience links are emitted for
      existing config dirs and pruned when stale.
- [x] There is one appearance cache entry per `(palette, skin, mode)`.
- [x] `appearanceHash` differs between modes and is stable for the same
      palette/skin/mode.
- [x] A wallpaper/palette change creates a new appearance while the skin
      cache is a hit; a skin change creates a new appearance while the
      palette cache is a hit.
- [x] v1 `skin.css`/`skin.json` pointers are still published.
- [x] Seed/reconcile write the generation in the same flow; stale appearance
      entries and convenience links are pruned; reruns are idempotent.
- [x] `inspect status` and `doctor` report appearance hash/skin/mode/pointer
      and watch health; doctor reports the v1/v2 skew.
- [x] `make gates` and `make gates-ci` pass.
- [x] Docs/CHANGELOG record the migration window and the v1-pointer removal
      follow-up.


## Evidence

Baseline: the runtime published palette and skin as two artifact sets with no
composed generation or `current/appearance` pointer (`src/runtime/adapters/seeder.py`
`repoint_current_symlinks`/`repoint_consumer_symlinks` before this ticket).

Implementation evidence on the ticket worktree (2026-11-06):

```sh
uv run pytest -q tests/unit/test_appearance.py
# 15 passed
```

The suite proves: generation layout + both schemas, `f(palette,skin,mode)`
identity (mode variants differ), palette/skin cache independence, the
single-hop pointer + stable convenience links, retained v1 skin pointers,
stale-entry/link pruning, idempotent republish, v2 mode-override resolution,
reconcile publication, inspect reporting and doctor skew detection.

```sh
make gates
# test       2272 passed, 3 skipped, 10 deselected
# lint       All checks passed
# contracts  54 passed, 1 skipped
# hermetic   2272 passed, 3 skipped, 10 deselected
# lock-check Resolved 25 packages
# docs-check exit 0
# make gates exit=0
```

```sh
make gates-ci
# same gate set; exit 0
```

## Implementation map

- `src/runtime/adapters/appearance.py` — staging, mode detection/resolution,
  v1→v2 compatibility translation, v2 manifest overrides, CSS composition,
  generation hash, manifest validation, pointer/read/prune helpers.
- `src/runtime/domain/models.py` — `AppearanceEntry`.
- `src/runtime/adapters/hashing.py` — canonical
  `sha256("appearance.v2\n" + paletteHash + "\n" + skinHash + "\n" + mode)`.
- `src/runtime/adapters/seeder.py` — `publish_appearance` (one repoint) and
  stale convenience-link pruning; v1 skin pointer paths untouched.
- `src/runtime/application/{seed_cache,reconcile}.py` — compose/publish in the
  same flow; fail-closed logging.
- `src/runtime/application/{inspect,doctor}.py` — appearance hash/skin/mode/
  pointer/watch reporting + v1/v2 skew detection.
- `src/runtime/adapters/appearance_ledger.py` + `ags_app_refresh.py` — ledger
  identity uses the generation hash with the recorded/derived mode.
- `src/runtime/adapters/contract_schemas.py`, `schemas/appearance-*.json` —
  vendored contracts v0.7.0 validators.
- `tests/unit/test_appearance.py`, updated schema-conformance and constructor
  tripwire tests.
