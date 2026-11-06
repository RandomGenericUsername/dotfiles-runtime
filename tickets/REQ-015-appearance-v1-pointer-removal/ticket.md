---
id: REQ-015
title: "Remove the v1 skin pointers after the appearance v2 cutover"
kind: requirement
status: done
lane: 3
links:
  openspec: none
  bmad: none
worktree: none
branch: none
---

# REQ-015 — Remove the v1 skin pointers after the appearance v2 cutover

> Folded into and closed by `REQ-018-systemd-aware-restart` (v0.9.3). The v2
> cutover is complete; apps consume `current/appearance` through
> `config/<configDir>/appearance.css`.

## Requirement

REQ-014 published the v2 appearance generation but intentionally kept the v1
pointer family so apps that still consumed the v1 plane never broke:

- `current/skins/<configDir>.css`
- `current/skin.json`
- `config/<configDir>/skin.css`
- `config/<configDir>/skin.json`

Remove that family now that every app consumes `current/appearance` through
`config/<configDir>/appearance.css` (contracts v0.7.0, waves 2–4 + the tuple
bump in wave 6). The removal must:

- stop creating `current/skins/` and `current/skin.json`;
- stop creating `config/<configDir>/skin.{css,json}` and prune the
  runtime-owned stale links, never a user file;
- keep the skin cache entries as the v2 generation's input (the v1 token map
  is read from the cache entry, not from a public pointer);
- flip `doctor`'s `appearance:v1v2-skew` item from the expected migration
  warning to a real failure if any runtime-owned v1 pointer reappears.

## Acceptance

- [x] No runtime code path publishes a v1 skin pointer.
- [x] Stale runtime-owned v1 links are pruned; user files/links are untouched.
- [x] `doctor` no longer reports the expected v1/v2 skew on a clean v2 host.
- [x] Docs/CHANGELOG record the final cutover and remove the migration note.
- [x] `make gates` and `make gates-ci` pass.

## Evidence / Verify

- Closed by `REQ-018-systemd-aware-restart` (v0.9.3, implementation commit
  `3648977`; v0.9.4 follow-up `fa92b2d` follows the live config-dir symlink);
  see that ticket for the full evidence and gate output.
- Verify: `uv run pytest -q tests/unit/test_skins.py
  tests/unit/test_appearance.py tests/unit/test_doctor_check.py` → `76 passed`.
