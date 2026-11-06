---
id: REQ-015
title: "Remove the v1 skin pointers after the appearance v2 cutover"
kind: requirement
status: proposed
lane: TBD
links:
  openspec: none
  bmad: none
worktree: none
branch: none
---

# REQ-015 — Remove the v1 skin pointers after the appearance v2 cutover

## Requirement

REQ-014 publishes the v2 appearance generation but intentionally keeps the v1
pointer family so apps that still consume the v1 plane never break:

- `current/skins/<configDir>.css`
- `current/skin.json`
- `config/<configDir>/skin.css`
- `config/<configDir>/skin.json`

Remove that family once every app consumes `current/appearance` through
`config/<configDir>/appearance.css` (contracts v0.7.0, waves 2–4 + the tuple
bump in wave 6). The removal must:

- stop creating `current/skins/` and `current/skin.json`;
- stop creating `config/<configDir>/skin.{css,json}` and prune the
  runtime-owned stale links, never a user file;
- keep reading a v1 generation long enough to migrate an already-provisioned
  host (one release window), then delete the v1 cache pointers;
- flip `doctor`'s `appearance:v1v2-skew` item from the expected migration
  warning to a real failure if any v1 pointer reappears.

## Acceptance

- [ ] No runtime code path publishes a v1 skin pointer.
- [ ] Stale runtime-owned v1 links are pruned; user files/links are untouched.
- [ ] `doctor` no longer reports the expected v1/v2 skew on a clean v2 host.
- [ ] Docs/CHANGELOG record the final cutover and remove the migration note.
- [ ] `make gates` and `make gates-ci` pass.
