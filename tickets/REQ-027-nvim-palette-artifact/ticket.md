---
id: REQ-027
title: "Publish the optional colors.nvim.lua palette artifact"
kind: requirement
status: done
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-027/
branch: ticket/REQ-027-nvim-palette-artifact
---

# Publish the optional colors.nvim.lua palette artifact

## Why

`REQ-123` makes Neovim consume the generated palette. `theme-producer` now
emits `colors.nvim.lua`; the runtime must copy it from the producer staging dir
and expose it at `current/colors.nvim.lua` without widening the existing
eight-artifact completeness contract (staged rollout).

## Requirement

1. Add `PALETTE_OPTIONAL_ARTIFACT_NAMES = ("colors.nvim.lua",)`.
2. `TprAdapter._copy_artifacts` copies each optional artifact when the producer
   emits it; absence is not an error.
3. The seeder publishes `current/colors.nvim.lua` when the palette entry has
   the file; absence is a silent skip (optional), unlike the required eight.
4. Existing `PALETTE_ARTIFACT_NAMES`, `PaletteArtifacts`, meta schema and cache
   identity remain unchanged.
5. No tags/releases/pins in this phase.

## Acceptance

- [x] Optional artifact tuple added.
- [x] tpr staging copy includes nvim when present.
- [x] `current/colors.nvim.lua` is published when present; absent is silent.
- [x] Existing eight-artifact tests remain green.
- [x] No tag/release/pin bump.

## Evidence

- `uv run pytest -q`: 2615 passed, 4 skipped; the only 2 failures are
  `tests/integration/test_phase5_reactive_runtime_e2e.py`, reproduced on
  untouched `main` (`76f19c0`) as pre-existing flakes.
- `PALETTE_ARTIFACT_NAMES` still the eight required names.


- Release: v1.6.0
