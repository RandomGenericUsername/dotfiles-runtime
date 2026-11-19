---
id: REQ-029
title: "Allow colors.nvim.lua in palette cache verification"
kind: requirement
status: done
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-029/
branch: ticket/REQ-029-nvim-optional-extra
---

# Allow colors.nvim.lua in palette cache verification

## Why

Runtime v1.6.1 requests `colors.nvim.lua`, but the staging verifier treats any
file not in `artifact_hashes` as corrupt, so `wallpaper set` fails with
`unrecorded file ... 'colors.nvim.lua'`.

## Requirement

1. `match_entry_artifacts` accepts an `allowed_extra` set.
2. Palette staging and entry verification allow `colors.nvim.lua`; any other
   unrecorded file remains corrupt.
3. Regression test covers the allow/deny pair.
4. Release `v1.6.2`.

## Acceptance

- [x] Optional artifact allowed; unexpected extra still rejected.
- [x] Unit test passes.
- [x] Full gates pass.
- [x] v1.6.2 tagged/pushed/released.


- Release: https://github.com/RandomGenericUsername/dotfiles-runtime/releases/tag/v1.6.2
