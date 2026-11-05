---
id: OPS-001
title: "Sync BUG-008 ticket state with its shipped release"
kind: ops-task
status: done
lane: direct
links:
  openspec: none
  bmad: none
worktree: .worktrees/OPS-001/
branch: ticket/OPS-001-ticket-status-sync
---

# Sync BUG-008 ticket state with its shipped release

## Work

`BUG-008` was fixed and released in `v0.5.2` (`81f57c5`), but the ticket still
had an unchecked live-verification box and a “not tagged” note, and
`tickets/INDEX.md` still said `in-progress`.

- Tick the live acceptance item; the ticket's own Outcome section records the
  two live wallpaper runs (`status: applied`, restart mode, exit 0, no
  “outside the deployed link” entries).
- Record the shipped tag in the implementation notes.
- Set the INDEX line to `done` with the `v0.5.2` tag and the 2026-11-04 live
  proof.

## Done-when

`BUG-008` frontmatter, acceptance boxes, implementation notes and INDEX all
agree that the fix shipped in `v0.5.2`.

## Evidence

- `tickets/BUG-008-bare-ags-run-discovery/ticket.md` — acceptance and Outcome.
- `tickets/INDEX.md` — `done`, `tag: v0.5.2`.
- `git merge-base --is-ancestor 8ef66b1 main` → true.
