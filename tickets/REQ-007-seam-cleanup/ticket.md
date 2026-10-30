---
id: REQ-007
title: "Seam cleanup: delete the icon-contrast guard, retire the CSG producer"
kind: requirement
status: proposed
lane: 2
links:
  openspec: none
  bmad: none
worktree: none
branch: none
---

# Seam cleanup: delete the icon-contrast guard, retire CSG

## Requirement

Two workarounds from the producer-transition era are removed outright — no
opt-outs, no fallbacks, no compatibility layer.

### 1. Icon-contrast guard: delete

Decision (2026-10-30, user-approved): the guard is a workaround at the wrong
layer; `on_backdrop` (theme-producer REQ-022) replaces it and the icons manifest
is re-pinned to roles (dotfiles-assets REQ-008). Delete completely:

- `src/runtime/domain/icon_contrast.py`
- `src/runtime/adapters/icon_contrast_sampler.py`
- `src/runtime/adapters/icon_contrast_prefs_store.py`
- `src/runtime/domain/icon_contrast_policy.py`
- `--contrast auto|on|off` on `wallpaper set` and `icons regenerate`; guard
  metadata in `meta.json` (`contrast.*`), inspect/doctor fields, and the
  per-wallpaper prefs store read/write.
- All guard tests/fixtures. Icons render exactly what the manifest resolves;
  no runtime retargeting of any placeholder.

The per-wallpaper prefs file may remain on disk unread (harmless) or be
removed; no migration code.

### 2. CSG producer: retire

Delete the CSG adapter, its producer enum/registry entry, contract shims,
tests and fixtures. `producer.json` keeps the seam for future producers but
accepts only registered, vocabulary-conformant producers (`tpr`); an unknown
producer is a typed error naming the supported set. Ambient
`RUNTIME__PALETTE__PRODUCER`/`..._TPR_ENGINE` stay ignored and reported.

Cross-repo cleanup (separate tickets referenced, not implemented here):
`dotfiles-v3/bootstrap.sh` rollback override, `dotfiles-provision` producer
template + csg pin.

## Acceptance

- [ ] `rg -i 'icon_contrast|icon-contrast' src/runtime` yields no behavior code
      (docs and history only).
- [ ] `rg -i 'csg|color-scheme-generator' src/runtime` yields no producer
      behavior code.
- [ ] `wallpaper set` / `icons regenerate` accept no contrast flag; `meta.json`
      has no `contrast` block; inspect/doctor no longer expose guard fields.
- [ ] Palette producer config rejects unknown/csg producer with a typed error.
- [ ] Analysis + unit + integration tests updated; `make gates` green; release
      `v0.5.1` (after REQ-006 and the assets re-pin land live).
- [ ] Live acceptance on the host: set two wallpapers (dark + light), icons
      render from roles only, doctor clean.

## Evidence / Verify

- Evidence: guard implementation paths listed above; CSG adapter
  `src/runtime/adapters/csg_adapter.py`; producer resolver
  `src/runtime/application/palette_producer.py`.
- Verify: `rg` commands above return zero behavior matches; `make gates`;
  `dotfiles-runtime doctor`.
