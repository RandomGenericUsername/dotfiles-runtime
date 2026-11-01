---
id: REQ-007
title: "Seam cleanup: delete the icon-contrast guard, retire the CSG producer"
kind: requirement
status: done
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-007/
branch: ticket/REQ-007-seam-cleanup
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

## Progress / Evidence (2026-10-31)

Status: implementation complete on `ticket/REQ-007-seam-cleanup`
(worktree `.worktrees/REQ-007/`), commit `1ce81cc`. Not tagged, pushed, or
merged.

### Removed

- Guard files: `domain/icon_contrast.py`, `domain/icon_contrast_policy.py`,
  `adapters/icon_contrast_sampler.py`, `adapters/icon_contrast_prefs_store.py`.
- Guard behavior: overlay/retargeting in `application/derive.py`, the
  `--contrast` flags and `icons preference` command, inspect/doctor
  `icons_contrast` fields, and the icons `meta.json` `contrast` block.
- CSG producer: `adapters/csg_adapter.py`, the `csg` resolver branch/default,
  and csg-specific tests/fixtures. Shared subprocess/artifact helpers moved to
  `adapters/producer_process.py` so `tpr_adapter` no longer imports the csg
  module. Monitor `bar_backdrop` sampling moved to
  `adapters/wallpaper_luminance.py` (not guard logic).
- Producer hash seed: `adapters/hashing.py` now only seeds `tpr`; unknown
  producers raise.

### Kept

- `palette.v1` fail-closed conformance before staging and before the visible
  swap (now unconditional: every selectable producer is conformant).
- `on_backdrop` is just a role; no runtime-specific logic.
- `producer.json` seam; only `tpr` accepted, unknown/csg raises typed
  `UnsupportedPaletteProducerError` naming `supported producers: tpr`.
  Ambient `RUNTIME__PALETTE__PRODUCER`/`..._TPR_ENGINE` remain ignored and
  reported.
- Watch roots now follow `config/theme-producer/templates` instead of the
  retired csg template tree.

### Acceptance evidence

- `rg -i 'icon_contrast|icon-contrast' src/runtime` → no matches.
- `rg -i 'csg|color-scheme-generator' src/runtime` → one documentation
  mention in `application/palette_producer.py` (the retired value in the
  typed-error docstring); no behavior code.
- `wallpaper set` / `icons regenerate` expose no `--contrast`; meta schema
  (embedded + `contracts/` copy, byte-identical) has no `contrast` block.
- Producer tests: `tests/unit/test_palette_producer_selection.py` asserts
  `csg`/unknown config and CLI values raise the typed error naming `tpr`.
- No-retarget regression:
  `tests/unit/test_regenerate_icons.py::TestRegenerateIconsUseCase::test_regenerate_renders_spine_mappings_without_retargeting`
  asserts the spine mappings path is what reaches the renderer and its hash is
  the entry's `input_mappings_hash`.
- `make gates` (run in the worktree, 2026-10-31) → exit 0:
  - `test`: 2167 passed, 3 skipped, 10 deselected
  - `lint`: All checks passed
  - `contracts-check`: 48 passed, 1 skipped
  - `hermetic`: 2166 passed, 3 skipped, 10 deselected
  - `lock-check`: Resolved 25 packages
  - `docs-check`: passed (silent)
- Full suite including integration (`uv run pytest -q`) → 2177 passed,
  3 skipped.

### Deviation / risk

- `adapters/icon_contrast_sampler.py` was relocated to
  `adapters/wallpaper_luminance.py` rather than dropped outright: the
  monitor `bar_backdrop` feature still needs top-strip luminance and is not
  the icon guard. Guard-only luminance/threshold math was deleted.
- A damaged live palette (complete entry missing an artifact, or
  nonconforming roles) now fails closed at reconcile instead of being
  silently regenerated; a fully missing entry dir still regenerates. This
  preserves the REQ-006 ordering and is covered by
  `test_missing_palette_artifact_fails_closed_before_swap`.
- Live host acceptance (two wallpapers + doctor) remains open; it needs the
  provisioned host and is not a stock-runner gate.

### Residual

- Live acceptance on the host is still pending.
- Cross-repo cleanup is out of scope here: `dotfiles-v3/bootstrap.sh`
  rollback override and `dotfiles-provision` producer template + csg pin.


## Outcome

Released `v0.5.1` and deployed live 2026-10-31 (PIN-0121):
- `rg -i 'icon_contrast|icon-contrast' src/runtime` → no matches; guard domain,
  adapter wiring, prefs store, policy, CLI `--contrast`, inspect/doctor fields
  and the `meta.json` contrast block are gone (schema copies updated);
- `rg -i 'csg|color-scheme-generator' src/runtime` → one docstring mention of
  the retired value in the typed-error contract; no behavior code; unknown
  producers raise `UnsupportedPaletteProducerError` naming `tpr`;
- live icons resolve exactly to manifest roles (battery bolt `#b9c3ff` =
  `primary`, bar ink `#e3e1e9` = `on_backdrop`), no retargeting;
- `doctor` clean (21 items), `inspect palette` conforming, `icons regenerate
  --help` has no `--contrast`;
- caveat recorded: the luminance sampler survives as
  `adapters/wallpaper_luminance.py` for the non-guard `bar_backdrop` monitor.
