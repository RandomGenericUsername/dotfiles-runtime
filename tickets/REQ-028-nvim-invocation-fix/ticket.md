---
id: REQ-028
title: "Request nvim.lua from tpr and pin the new invocation contract"
kind: requirement
status: done
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-028/
branch: ticket/REQ-028-nvim-invocation-fix
---

# Request nvim.lua from tpr

## Why

Runtime v1.6.0 could copy `colors.nvim.lua` but still invoked tpr with only the
eight legacy formats, so tpr never emitted it and the host fell back to
Catppuccin.

## Requirement

1. Pin `cli-invocation-contract` `v0.13.0`.
2. Add `--format nvim.lua` to `TprAdapter`'s argv.
3. Release `v1.6.1`.

## Acceptance

- [x] Contract pin is v0.13.0.
- [x] TprAdapter requests nvim.lua.
- [x] Tests/gates pass.
- [x] v1.6.1 tagged/pushed/released.


- Contract v0.13.0: https://github.com/RandomGenericUsername/contracts/releases/tag/v0.13.0
- Release: https://github.com/RandomGenericUsername/dotfiles-runtime/releases/tag/v1.6.1
