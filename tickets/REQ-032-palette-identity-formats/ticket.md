---
id: REQ-032
title: "Include the requested format set in the palette cache identity"
kind: requirement
status: done
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-032/
branch: ticket/REQ-032-palette-identity-formats
---

# Include the requested format set in the palette cache identity

## Why

Runtime v1.6.1 started requesting `nvim.lua` from tpr, but the palette entry
hash only covered wallpaper, template set, engine, version and mode. The old
eight-artifact palette entry therefore stayed a cache hit after the requested
artifact set grew, so `colors.nvim.lua` was never produced until the entry was
manually evicted.

Evidence: `src/runtime/adapters/hashing.py:331-365` (pre-change seed formula)
folded engine/version/templates/mode only; `src/runtime/adapters/tpr_adapter.py`
built the `--format` argv inline and `expected_entry_hash` omitted it.

## Requirement

1. The requested format set participates in the palette cache identity via a
   canonical, order-insensitive digest (sort the names before hashing).
2. `TprAdapter` keeps one source of truth for the format list (`TPR_FORMATS` /
   `TprAdapter.formats`) and uses it for both argv construction and
   `expected_entry_hash`.
3. The adapter still validates `output_dir.name == expected_entry_hash`.
4. Tests prove: same inputs + same set is stable; a changed set is a different
   hash; format order does not change the hash; the adapter identity reflects
   its declared set.
5. Document the identity change under CHANGELOG `Unreleased` without bumping
   the release version.

## Acceptance

- [x] Format set is folded into the producer seed through a sorted set digest.
- [x] `TprAdapter.formats` drives both argv and `expected_entry_hash`.
- [x] `validate_output_dir` check against `expected_entry_hash` unchanged.
- [x] Unit tests cover stability, set change, order insensitivity and adapter
      reflection of the declared set.
- [x] Targeted tests, `make lint` and `make gates` pass on the ticket branch.
- [x] CHANGELOG `Unreleased` documents the change; no version bump.


- Release: v1.6.4
