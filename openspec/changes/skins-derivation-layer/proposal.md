# skins-derivation-layer

## Why

The desktop currently derives colors, effects and icons, but every app still hard-codes
paint/rhythm decisions (blur, border weight, radii, spacing) that the owner wants to select as a
**skin**. The frozen skin spec v1 (GTK 4.24 probes) fixes the scope: a skin is a palette/ink
mapping + paint + rhythm, never structure or variants, and it must be palette-independent so a
wallpaper change does not re-expand CSS.

Without a runtime-owned skin layer, each consumer would invent its own cache, pointer and
failure policy. This change adds the same content-addressed, atomically repointed derivation
layer the runtime already owns for palette/effects/icons.

Evidence (baseline):

```sh
rg -n "skin" src/runtime --glob '*.py'   # no matches
```

## What Changes

- Add `SkinEntry`/`SkinUnavailableError` domain types and a `SkinExpander` port/adapter that
  validates JSON or TOML manifests and expands `{{SKIN_*}}` templates.
- Add `cache/skins/<sha256>/` with `meta.json`; key = `sha256(templates_hash ‖ manifest_hash)`.
- Add `current/skins/<app>.css` + `current/skin.json` links and AGS consumer pointers
  `config/ags/skin.css` / `config/ags/skin.json`.
- Add `skin` to `desired.json` (default `glass`) and to `current.json`.
- Fold the skin entry hash into the appearance hash; keep the bar's restart lane unchanged.
- Fail closed per skin: bad/missing assets keep the previous skin, log structurally, and never
  touch app CSS.
- Extend inspect/doctor/verify/prune coverage and docs/tests.

## Capabilities

### New Capabilities

- `skin-derivation` — selection, manifest/template expansion, cache identity, pointers,
  appearance participation and fail-closed behavior.

### Modified Capabilities

- `repo-harness` — no behavioral change; the gate set and release ritual still apply.

## Non-goals (out of scope)

- No structure/variant tokens; no component geometry or widget-tree templates.
- No Jinja/templating dependency; only deterministic `{{SKIN_*}}` substitution.
- No asset-authoring changes in this repo (`dotfiles-assets` ships the v1 manifests/templates).
- No new CLI command beyond inspect/doctor/cache coverage.
- No changes to wallpaper/palette/effects/icons cache keys.

## Impact

- `src/runtime/{domain,ports,adapters,application,cli}` gain the skin layer.
- Embedded/canonical `current`, `meta` and `history` schemas gain optional skin fields.
- `dotfiles-v3` must pin `v0.7.0`; provisioning must install the assets skin tree and create
  `config/ags/` (already exists) — no code change in this repo.
- Existing caches remain valid; new `cache/skins/` entries are additive.
