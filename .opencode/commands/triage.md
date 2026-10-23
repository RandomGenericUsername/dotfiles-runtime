---
description: "Triage a request: classify kind first, size second, route to the cheapest safe lane"
---

# /triage — task router

Route every request to the cheapest safe lane. Kind determines the lane; size determines ceremony within it.

**Provided arguments**: $ARGUMENTS (the request to triage)

## 0. Mandatory pre-steps (never skip)

1. **Evidence rule** (AGENTS.md §1): every non-trivial claim in triage output carries an `Evidence:` line (file path + line range, commit SHA, or URL + quoted section; or a run command + observed result) and a `Verify:` line (one command to reproduce). If evidence does not exist yet, write `Evidence: MISSING — run <X> to produce it` and do not present the claim as fact.
2. **Worktree guard**: before writing any ticket, change, or code, run `scripts/worktree-guard.sh`. If main is dirty it aborts — clean main first. All implementation happens on a `ticket/<id>-<slug>` worktree/branch; main stays untouched. Record worktree + branch in the item. Never edit the main checkout directly.

## 1. Classify kind first

Assign exactly one `kind:` from `epic | requirement | bug | ops-task | spike | task` (ticket-system spec: fixed kind vocabulary). A small epic still decomposes — never build an epic directly. A large bug still follows the bug lane (repro → fix → regression test).

- **bug without `repro:` + `observed:` + `expected:`**: reject outright, no parking state. Tell the user what is missing.
- **`task`**: requires a `parent:` (epic/requirement id). A task never routes to `bmad-advisory` on its own — it inherits the parent's lane, and decomposing a parent into tasks changes no lane. A parentless `task` is rejected at validation.
- **`spike`**: propose a per-spike `timebox:` plus named `questions:` at triage. It stops at the box and reports `answered:` / `open:`; implementation output from a spike is a failure.

## 2. Lane rubric (size + uncertainty second)

| Lane | Maps to | When |
|------|---------|------|
| 1. XS direct | ticket (+ fix directly) | Trivial, fully understood, one small edit: typo, one-line fix, config value |
| 2. openspec-single | one item + one OpenSpec change | Single bounded feature, understood, fits one proposal/spec/design/tasks slice |
| 3. ticket-multi, no BMAD | one item + N OpenSpec changes | Multi-part but fully understood, no architectural unknowns — BMAD SHALL NOT be invoked |
| 4. bmad-advisory | BMAD specialist advice → spec → change(s) | Broad, uncertain, or architectural; needs exploration before planning |

## 3. Explicit ask on ambiguity

When classification or size is ambiguous (e.g. one change vs several, BMAD vs plain split), STOP and ask the user with chosen options plus your recommendation — before writing any item or change. Never assume.

For epics / uncertain work, recommend BMAD vs plain split with reasons and let the user select.

## 4. BMAD advisory never auto-executes

BMAD specialists give advice only. Their output never triggers implementation on its own. After advice lands: write/adjust the spec, then changes, then wait for an explicit user request before any apply/build step. If the user selects plain decomposition, write the epic with children and require no BMAD artifacts.

## 5. Output

For each triaged request, emit:

- `kind:` + why (one line)
- `lane:` (1–4 above) + why
- If ambiguous: options + recommendation, then wait
- If `spike`: proposed `timebox:` + `questions:`
- If `task`: `parent:` + inherited lane
- `Evidence:` / `Verify:` lines per the evidence rule
- Next step (ticket file path, change name, or guard command) — do not start it without user confirmation when ambiguity was involved
