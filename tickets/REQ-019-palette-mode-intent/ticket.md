---
id: REQ-019
title: "Palette mode intent: desired.json mode -> tpr settings env -> cache identity"
kind: requirement
status: in-progress
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-019/
branch: ticket/REQ-019-palette-mode-intent
---

# REQ-019 — Palette mode intent: desired.json mode -> tpr settings env -> cache identity

## Requirement

`theme-producer` v0.6.0 supports `--mode dark|light|smart`, reads
`settings.output.default_mode` from the environment, and emits the resolved
`mode` in `colors.yaml`. The runtime never passes a mode, so every palette is
dark even though the appearance plane already selects `mode.dark`/`mode.light`
from the palette bytes.

The runtime must make the mode a first-class desired-state intent and carry it
through the producer and cache identity:

1. `desired.json` gains optional `mode: "dark" | "light" | "smart"`, default
   `smart`. `DesiredState`, `desired_state_reader` validation (including the
   unknown-key rule) and docs reflect it. Every apply/reconcile/derive
   composition root that reads desired state resolves and uses the mode.
2. The tpr adapter passes the chosen mode in its child env as
   `THEME__OUTPUT__DEFAULT_MODE=<mode>` alongside the existing `THEME__*`
   settings overrides. argv does not change.
3. The mode is part of the palette entry hash, so dark/light/smart can never
   collide. The invalidation is documented. The appearance hash stays
   `f(palette, skin, mode)`; the palette's emitted `mode` remains the source
   for appearance mode.
4. Tests cover desired parse/validation (default `smart`, invalid rejected,
   unknown key still fails), the adapter env, palette hash difference/stability
   across modes, smart/light pass-through, the intent reaching reconcile/apply,
   and cache hit/miss behavior.
5. README/CHANGELOG/docs and release metadata advance to `v0.9.5`; gates pass.

## Acceptance

- [ ] `desired.json` accepts an optional `mode` in `dark|light|smart`; absent
      means `smart`; any other value is rejected and unknown keys still fail.
- [ ] `DesiredState` carries the mode and all desired-state consumers agree on
      the default.
- [ ] The tpr child env contains `THEME__OUTPUT__DEFAULT_MODE` for the resolved
      mode, while argv is unchanged.
- [ ] `palette_entry_hash` identity differs across `dark`/`light`/`smart` and
      is stable for repeated resolution within one mode.
- [ ] Apply/reconcile/derive composition roots resolve the intent mode and use
      it for palette production; a mode change produces a cache miss and a
      repeat within one mode is a cache hit.
- [ ] Appearance identity remains `f(palette, skin, mode)`, with the palette's
      emitted mode as the appearance mode source.
- [ ] Tests prove the parse rules, adapter env, hash behavior, pass-through,
      intent wiring and cache hit/miss behavior.
- [ ] README/CHANGELOG/docs describe the intent and the palette cache
      invalidation; version metadata is `0.9.5`.
- [ ] `make gates` and `make gates-ci` pass; release tag `v0.9.5` is created
      after the green commit and merged fast-forward to `main`.

## Evidence

Baseline on `main` @ `44a0879` (v0.9.4):

- `src/runtime/adapters/desired_state_reader.py:86` allows only
  `{"version", "wallpaper", "keep", "pinned", "skin"}`; no `mode`.
- `src/runtime/adapters/tpr_adapter.py:262-273` builds the child env with
  output/engine/runtime/templates overrides; no `THEME__OUTPUT__DEFAULT_MODE`.
- `src/runtime/adapters/hashing.py:278-344` seeds the palette entry key with
  producer/engine/version/templates; the mode is absent.
- `src/runtime/domain/models.py:418-432` `DesiredState` has no `mode`.
- `src/runtime/application/appearance.py:603-606` already derives appearance
  identity from the palette-emitted mode, so the missing producer input is the
  only dark-only gap.

Verify baseline:

```sh
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
rg -n 'unknown = set\(data\)|THEME__OUTPUT__DIRECTORY|palette_entry_hash_for_producer|class DesiredState' \
  src/runtime/adapters/desired_state_reader.py \
  src/runtime/adapters/tpr_adapter.py \
  src/runtime/adapters/hashing.py \
  src/runtime/domain/models.py
```

## Implementation map

- `src/runtime/domain/models.py` — `DesiredMode` + default `smart` on
  `DesiredState`.
- `src/runtime/adapters/desired_state_reader.py` — optional-mode validation.
- `src/runtime/adapters/hashing.py` — mode folded into the tpr producer seed
  and the producer-aware palette entry hash.
- `src/runtime/adapters/tpr_adapter.py` — validated `mode`, child env
  `THEME__OUTPUT__DEFAULT_MODE`, mode-aware entry key.
- `src/runtime/application/palette_producer.py`,
  `src/runtime/cli/main.py` — resolve the intent mode at every composition root
  that produces palettes.
- `src/runtime/application/reconcile.py` — a mode-aware producer's expected
  palette key wins over a stale recorded palette entry.
- `tests/unit/test_desired_state.py`, `test_tpr_adapter.py`,
  `test_palette_producer_hash.py`, `test_reconcile.py`, `test_seed_cache.py`,
  `test_cli_producer_config.py` — coverage for every acceptance item.
- `README.md`, `CHANGELOG.md`, `docs/ARCHITECTURE.md`, `pyproject.toml` — docs,
  release note and version.
