# Ticket lifecycle

Item files live at `tickets/<id>-<slug>/ticket.md`. Every status change MUST update the matching `tickets/INDEX.md` line in the same commit, or the completeness check fails naming the diverged entry.

Gate commands: `rg` one-liners below run from the repo root; the worktree guard is `bash scripts/worktree-guard.sh`.

## epic (container only — never builds directly)

`proposed` → `in-progress` → `done`

- Gate `proposed → in-progress`: `rg "^children:" tickets/<epic-dir>/ticket.md` non-empty (decomposition exists) AND `bash scripts/worktree-guard.sh --check-clean` exit 0.
- Gate `in-progress → done`: rollup holds — every child `done` AND `done-when` holds; gate: `rg "^- \[x\] " tickets/<epic-dir>/ticket.md` count equals `children:` count. Marking done with any child not done is rejected.
- Epic links no execution artifacts directly (no OpenSpec change, no build plan); gate: `rg "^  (openspec|bmad): none$" tickets/<epic-dir>/ticket.md`.

## requirement

`proposed` → `in-progress` → `done`

- Gate `proposed → in-progress`: router lane assigned (`/triage` output recorded in `lane:`) AND `bash scripts/worktree-guard.sh --check-clean` exit 0.
- Gate `in-progress → done`: every acceptance checkbox ticked; linked OpenSpec change (if any) validated: `openspec validate --change <name>` exit 0.

## bug (rejected without repro — no parking state)

`triage` → `in-progress` → `done` | `triage` → `rejected`

- Gate `→ triage` (entry): `repro:` + `observed:` + `expected:` all present; gate: `rg "^(repro|observed|expected): .+" tickets/<bug-dir>/ticket.md` returns 3 lines. Missing any one → rejected outright with the missing fields named.
- Gate `triage → in-progress`: `severity:` recorded.
- Gate `in-progress → done`: fix + regression test covering the `repro:` command pass.
- `rejected`: terminal; records the missing-repro (or duplicate/wontfix) reason.

## ops-task (readable as `chore`; file declares `ops-task`)

`proposed` → `in-progress` → `done`

- Gate `proposed → in-progress`: `bash scripts/worktree-guard.sh --check-clean` exit 0 (trivial ops tasks skip OpenSpec/BMAD ceremony, never git discipline). No `parent:` allowed; gate: `rg "^parent:" tickets/<ops-dir>/ticket.md` returns nothing.
- Gate `in-progress → done`: `done-when` holds on the ticket branch, merged back.

## spike (per-spike box; stops at the box)

`boxed` → `reported`

- Gate `→ boxed` (entry): `timebox:` + `questions:` present; gate: `rg -L "^timebox:" tickets/spike-*/ticket.md` returns nothing.
- Gate `boxed → reported`: `answered:` / `open:` findings written; no second box without an explicit user call. INDEX timebox state flips `boxed <limit>` → `reported`.
- Implementation output from a spike is a failure — a spike never transitions to `done` with build artifacts.

## task (child unit — parent REQUIRED, lane inherited)

`proposed` → `in-progress` → `done`

- Gate `→ proposed` (entry): `parent:` non-empty naming an existing story/epic id; gate: `rg -L "^parent: .+" tickets/task-*/ticket.md` returns nothing. A parentless task is rejected at validation regardless of all other fields.
- Gate `proposed → in-progress`: parent's lane applies (`lane: inherits`); never routed to `bmad-advisory` on its own; `bash scripts/worktree-guard.sh --check-clean` exit 0.
- Gate `in-progress → done`: `done-when` holds; parent rollup updated.
