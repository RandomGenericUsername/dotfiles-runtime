---
id: REQ-031
title: "Add Neovim to the runtime reloader chain"
kind: requirement
status: done
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-031/
branch: ticket/REQ-031-nvim-reloader
---

# Add Neovim to the runtime reloader chain

## Why

Runtime publishes the optional `colors.nvim.lua` palette artifact (REQ-027)
and requests it from tpr (REQ-028), but the converge reload chain never tells
a running Neovim to re-read it. Only kitty and btop are signalled today.

## Requirement

1. `src/runtime/adapters/nvim_reloader.py` implements `IDesktopReloader`.
2. It mirrors `KittyReloader`/`BtopReloader`: injectable `pid_source`
   (default `pgrep -x nvim`) and `signaller` (default `os.kill`), sends
   `SIGUSR1`, returns vacuous success when no nvim is running, surfaces a
   failed signal as `False`, and bounds the `pgrep` timeout.
3. `_build_reloaders()` in `src/runtime/cli/main.py` appends
   `NvimReloader()` after `BtopReloader`.
4. Unit tests cover none/one/many/failure/injection plus a wiring/order test.
5. Targeted tests, `make lint`, and `make gates` are green.

## Acceptance

- [x] No nvim => vacuous `True`; one/many => exactly one `SIGUSR1` each.
- [x] Failed signal => `False`; a partial failure fails the whole reload.
- [x] Default source runs `pgrep -x nvim` with a bounded timeout and
      degrades to `[]` on missing/hung pgrep.
- [x] `_build_reloaders()` order ends Hyprland, [terminal], kitty, btop, nvim.
- [x] Targeted tests, `make lint`, `make gates` pass.

## Notes

- `REQ-030` worktree/branch was already taken by in-progress work, so this
  ticket uses the next free id.
- Ticket left `in-progress` per instruction; no release/tag/merge.


- Release: v1.6.3
