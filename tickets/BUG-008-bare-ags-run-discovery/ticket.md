---
id: BUG-008
title: "Bar is not updated: discovery skips the bare `ags run` bar process"
kind: bug
status: in-progress
lane: 2
links:
  openspec: none
  bmad: none
worktree: none
branch: none
severity: high
---

# Bar refresh fails with “process outside the deployed link”

## Evidence

- Wallpaper set succeeds but the notification reports:
  `Could not update: bar (ags, unknown): AGS name 'ags' is owned by a process
  outside the deployed link 'ags'`.
- The bar runs as bare `ags run` (autostart via `ags-run-locked
  --expect-instance ags`), with no `-d`; `process.lister` in
  `adapters/ags_reloader.py` only records `ags run -d <dir>` and skips
  processes without `-d`.
- `AppDiscovery` therefore sees the `ags` name owned but no matching process and
  emits the failure at `adapters/ags_app_discovery.py:250-256`, so no restart.
- All other apps launch with `-d` and refresh normally.

## Requirement

Attribute a bare `ags run` to its default config dir
(`$XDG_CONFIG_HOME/ags`, default `~/.config/ags`) so discovery resolves it to
the deployed link (the link chain resolves to the app version) and the bar can
be restarted like every other consumer. Do not weaken the foreign-process
protection: a process whose resolved config dir is NOT the deployed link must
still fail closed.

## Acceptance

- [ ] Bare `ags run` is discovered with config dir `~/.config/ags` and
      instance `ags`; resolved link match succeeds.
- [ ] Tests: bare run attributed; `-d` behavior unchanged; a genuinely foreign
      `ags run` (different config dir) still fails.
- [ ] Live: change wallpaper → bar restarts and adopts the new palette with no
      “outside the deployed link” notification.
- [ ] Gates green; release v0.5.2 after review.
