---
id: REQ-003
title: "Palette producer seam: adopt tpr behind the CSG default"
kind: requirement
status: in-progress
lane: 4
links:
  openspec: none
  bmad: architecture-theme-producer
worktree: .worktrees/REQ-003/
branch: ticket/REQ-003-palette-producer-seam
---

# Palette producer seam: adopt tpr behind the CSG default

## Requirement

Make the runtime able to produce a palette with `tpr` as well as `csg`, without changing the
default (`csg`) and without breaking existing cache keys.

The producer seam:

- `RUNTIME__PALETTE__PRODUCER=csg|tpr` (default `csg`) at the composition roots.
- `TprAdapter` implements `IColorSchemeGenerator`, invokes the pinned `tpr` record
  (`generate <wallpaper> --format ...` x8, `THEME__OUTPUT__DIRECTORY`/`THEME__OUTPUT__OVERWRITE`),
  copies the 8 consumer artifacts into the palette entry, and never copies `theme.json`.
- `build_env` allows the `THEME__` prefix.
- The palette entry hash includes a producer fingerprint: `csg` keeps the existing
  `sha256(wallpaper_hash || template_set_hash)` (seed `None`); `tpr` adds
  `sha256("tpr:" + engine + ":" + tpr_version + ":" + template_set_hash)` as the seed.
- `derive.ensure_palette` and `invalidation.recomputed()` use the same fingerprint and the
  producer-specific templates dir (`csg_templates` vs `tpr_templates`).
- `cli-invocation-contract` is re-pinned to `v0.4.0`.

Out of scope: provisioning, the default flip, AGS/ITR changes, and any CSG change.

## Acceptance

- [ ] `RUNTIME__PALETTE__PRODUCER` selects the producer; absent = `csg`; an unknown value fails
  before any cache read.
- [ ] `TprAdapter` writes exactly the 8 artifacts, hashes them, and rejects a missing/extra
  contract artifact; `theme.json` is ignored if present.
- [ ] CSG palette keys are byte-identical to today (seed `None`); TPR keys differ per engine,
  tpr version and templates hash; switching producer cannot hit the other's entry.
- [ ] `invalidation.recomputed()` returns the same key the producer would use.
- [ ] `THEME__` env keys pass `build_env`; secrets are still blocked.
- [ ] `make gates` green; the contract pin check sees v0.4.0.

## Evidence

- `theme-producer/tickets/spike-001-dotfiles-fit/ticket.md` — adapter shape, hash blindness,
  consumer pointers, rollback.
- `contracts` `v0.4.0` `tpr` record — argv/env/artifacts/timeout.
- `dotfiles-runtime/src/runtime/adapters/{csg_adapter,env,invalidation}.py`,
  `src/runtime/application/derive.py:680-745`, `src/runtime/cli/main.py` composition sites.
