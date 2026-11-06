---
id: REQ-014
title: "Publish one appearance generation and one atomically repointed pointer"
kind: requirement
status: in-progress
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

- [ ] `cache/appearance/<appearanceHash>/` holds `colors.yaml`,
      `tokens.json`, `appearance.json` and one `<configDir>.css` per target.
- [ ] `tokens.json` validates against `appearance-json.schema.json`;
      `appearance.json` validates against `appearance-artifact.schema.json`.
- [ ] `current/appearance` is a single-hop symlink repointed atomically.
- [ ] `config/<configDir>/appearance.css` convenience links are emitted for
      existing config dirs and pruned when stale.
- [ ] There is one appearance cache entry per `(palette, skin, mode)`.
- [ ] `appearanceHash` differs between modes and is stable for the same
      palette/skin/mode.
- [ ] A wallpaper/palette change creates a new appearance while the skin
      cache is a hit; a skin change creates a new appearance while the
      palette cache is a hit.
- [ ] v1 `skin.css`/`skin.json` pointers are still published.
- [ ] Seed/reconcile write the generation in the same flow; stale appearance
      entries and convenience links are pruned; reruns are idempotent.
- [ ] `inspect status` and `doctor` report appearance hash/skin/mode/pointer
      and watch health; doctor reports the v1/v2 skew.
- [ ] `make gates` and `make gates-ci` pass.
- [ ] Docs/CHANGELOG record the migration window and the v1-pointer removal
      follow-up.
