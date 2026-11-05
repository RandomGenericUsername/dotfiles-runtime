---
id: BUG-008
title: "Bar is not updated: discovery skips the bare `ags run` bar process"
kind: bug
status: done
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-008/
branch: ticket/BUG-008-bare-ags-run-discovery
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

- [x] Bare `ags run` is discovered with config dir `~/.config/ags` and
      instance `ags`; resolved link match succeeds.
- [x] Tests: bare run attributed; `-d` behavior unchanged; a genuinely foreign
      `ags run` (different config dir) still fails.
- [x] Live: change wallpaper → bar restarts and adopts the new palette with no
      “outside the deployed link” notification (proof in Outcome below).
- [x] Gates green; version bumped to 0.5.2 and released as `v0.5.2` (`81f57c5`).

## Implementation

- worktree: `.worktrees/BUG-008/`
- branch: `ticket/BUG-008-bare-ags-run-discovery`
- fix commit: `8ef66b1` — `BUG-008: attribute bare ags run to its default config dir`
- version: `0.5.2` (`pyproject.toml`, `runtime.__version__`, `uv.lock`); released
  as annotated tag `v0.5.2` (`81f57c5`).

### Attribution design

- `adapters/ags_reloader.py::_discover_ags_apps` still records explicit
  `-d/--directory` processes exactly as before.
- A bare `ags run` is attributed to the effective default config dir read from
  `/proc/<pid>/environ`: `$XDG_CONFIG_HOME/ags` when non-empty, else the
  process's `$HOME/.config/ags`, else the runtime's `~/.config/ags` when the
  environment is unreadable/empty.
- Instance is `_instance_name_for_dir(config_dir)` (bar `app.tsx` →
  `ags`), falling back to `BAR_INSTANCE_NAME` for a bare run.
- The ownership rule is untouched: `AppDiscovery` resolves the attributed
  config dir and compares it to the deployed link. A bare run whose config dir
  does not resolve to that link is still a structured foreign/outside failure.
- `AgsReloader.reload()` now skips the discovered bar instance (`ags`) because
  the bar is restarted unconditionally; this avoids a duplicate quit/launch.

### Evidence

- `uv run pytest -q tests/unit/test_ags_reloader.py tests/unit/test_ags_app_discovery.py -m "not integration"`
  → `78 passed`.
- `make gates` →
  test `2178 passed, 3 skipped, 10 deselected`; lint `All checks passed!`;
  contracts-check `48 passed, 1 skipped`; hermetic
  `2178 passed, 3 skipped, 10 deselected`; lock-check `Resolved 25 packages`;
  docs-check passed.

## Live verification (pending orchestrator deploy)

Deploy commit `8ef66b1` (v0.5.2), then:

1. `dotfiles-runtime wallpaper set <image>`
2. Find the newest AGS refresh run:

   ```sh
   run=$(ls -t "$XDG_STATE_HOME/dotfiles/logs/ags-app-refresh" | head -1)
   jq -c 'select(.instance=="ags") | {status, reason, argv}' \
     "$XDG_STATE_HOME/dotfiles/logs/ags-app-refresh/$run"/*.jsonl
   ```

   Expected: one `ags` record with `"status":"applied"`; no reason containing
   `outside the deployed link` or `foreign/dev process`.
3. Confirm the bar visibly adopts the new palette and no
   `Could not update: bar (ags, unknown)` notification appears.


## Outcome

Fixed in `v0.5.2` and deployed via PIN-0129. Live proof 2026-11-04:
- wallpaper default → eagle: refresh log
  `20261104T131302Z-…/00-ags.jsonl` → `status: applied`, `mode: restart`,
  argv `ags run -d …/config/ags`, exit 0; palette changed to `9810b297…`;
- wallpaper eagle → default: `20261104T131315Z-…/00-ags.jsonl` → same
  `applied`/restart/exit 0;
- zero `outside the deployed link` / `foreign/dev process` entries.
