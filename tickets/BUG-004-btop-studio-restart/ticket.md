---
id: BUG-004
title: "btop-theme-studio is restarted by AgsReloader and loses unsaved edits"
kind: bug
status: done
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-004/
branch: ticket/BUG-004-btop-studio-restart
repro: "uv run pytest -q tests/unit/test_ags_reloader.py::TestAgsReloaderStandaloneTools::test_skips_btop_studio_dir"
observed: "On the pre-fix tree the regression test fails: subprocess.run is called twice (bar quit plus `ags quit -i btop-theme-studio`) and Popen twice (bar run plus `ags run -d .../ags-btop-studio`). A running studio is quit and relaunched by reconcile; pending edits live only in memory and are lost."
expected: "Only the bar is quit/relaunched. `ags-btop-studio` is left running because its pending edits are in memory; the skip applies to every AgsReloader reload cycle and no live refresh is required for this safety fix."
---

# btop-theme-studio is restarted by AgsReloader and loses unsaved edits

## Triage (`/triage`, 2026-10-26)

- `kind: bug` — a wallpaper change destroys in-memory pending edits; the
  behavior violates the "never restart an editor with unsaved state" rule.
- `lane: 2 — one bounded fix` — add the deployed config-dir name to
  `DEFAULT_SKIP_CONFIG_DIRS` and pin it with one regression test. No live
  refresh and no architectural change in this ticket.
- `severity: high` — the loss is irreversible and silent; the trigger is narrow
  (studio open with pending edits), but the fix is small and the rule is hard.
- Guard: `bash scripts/worktree-guard.sh check-clean` exited 0 before this
  worktree was created.
- Deployed name: `~/.config/ags-btop-studio` is the config dir; the AGS
  instance is `btop-theme-studio`. The skip check uses the **config-dir name**,
  so the entry must be `ags-btop-studio`, not `btop-theme-studio`.

Evidence: skip set at `src/runtime/adapters/ags_reloader.py:61-63`; the
studio's in-memory pending map at
`btop-theme-studio/ui/StudioWindow.tsx:124,450-462`; the app is deployed as
`$HOME/.config/ags-btop-studio` (launcher `DIR=` and `readlink -f`); the
spike's btop gap in
`ags-apps/tickets/SPIKE-001-ags-refresh-patterns/ticket.md`.
Verify: the `repro` command above; the pre-fix prototype in
`ags-apps/tickets/SPIKE-001-ags-refresh-patterns/ticket.md` shows the same
quit/run pair.

## Repro

```bash
# GUI-free regression test (added in this ticket):
uv run pytest -q tests/unit/test_ags_reloader.py::TestAgsReloaderSkip::test_skips_btop_studio_dir

# Host-visible repro (do not drive the GUI in this task):
# 1. Open btop-theme-studio (SUPER+B).
# 2. Change a theme value; leave it pending (do not save).
# 3. Run: dotfiles-runtime wallpaper set <image>
# 4. Observe: the process is gone and the edit is not in any file.
```

## Observed

- Pre-fix regression test: `subprocess.run` is called for the bar and for
  `ags quit -i btop-theme-studio`; `Popen` is called for the bar and for
  `ags run -d /home/u/.config/ags-btop-studio`.
- Host behavior: a wallpaper change restarts the running studio; the pending
  map is in memory and is not written to disk.
- The skip set currently contains only the three live apps:
  `ags-icme`, `ags-wallpaper-selector`, `ags-notifications`.

## Expected

- `ags-btop-studio` is left running; no `ags quit` or `ags run` is issued for
  it by `AgsReloader`.
- The fix is skip-only: the studio may keep stale chrome/palette until it is
  reopened. Live refresh and pending-aware semantics are the separate
  `EPIC-002` / `REQ-010` work.
- A no-op reconcile and a real palette change both leave the studio process
  alone.

## Fix notes

- Add `ags-btop-studio` to `DEFAULT_SKIP_CONFIG_DIRS` in
  `src/runtime/adapters/ags_reloader.py`.
- Extend the skip-set comment: the studio is an editor with unsaved pending
  edits; a wallpaper change must not discard them.
- Add `test_skips_btop_studio_dir` in `tests/unit/test_ags_reloader.py`,
  mirroring the ICME/selector/notifications skip tests: only the bar is
  quit/relaunched.
- The docs row (`dotfiles-v3/docs/ags-restart-vs-live-theme-refresh.md`) and
  the live-refresh path are deliberately out of scope; they belong to
  `EPIC-002` / `REQ-010`.

## Acceptance

- [x] `DEFAULT_SKIP_CONFIG_DIRS` contains `ags-btop-studio`.
- [x] The regression test fails before the change and passes after.
- [x] No other app's skip/restart behavior changes.
- [x] `make test` (or the focused pytest) is green.
- [x] INDEX status and closing evidence are updated in the closeout commit.

## Closing evidence

- **Plan:** `d3f24f4 plan(BUG-004): add the btop studio unsaved-edit restart bug`.
- **Red:** `2cd2c2e test(BUG-004): pin the btop studio skip`.
  `uv run pytest -q tests/unit/test_ags_reloader.py::TestAgsReloaderStandaloneTools::test_skips_btop_studio_dir`
  → `1 failed`; `subprocess.run` called twice, including
  `ags quit -i btop-theme-studio`.
- **Fix:** `87245f2 fix(BUG-004): skip btop-theme-studio on palette reload`.
  `DEFAULT_SKIP_CONFIG_DIRS` now contains the deployed name
  `ags-btop-studio`.
- **Green:** `uv run pytest -q tests/unit/test_ags_reloader.py`
  → `29 passed in 0.33s`.
- **Gates:** `make gates-ci` → exit 0; `2127 passed, 3 skipped, 20 deselected`
  in both runs; `lint`, `contracts-check`, `hermetic`, `lock-check` and
  `docs-check` green.
- **Scope:** skip-only safety. Live refresh and the btop docs row remain
  `EPIC-002` / `REQ-010` in `ags-apps`; they are not implemented here.

Verify: the `repro` command above is green on this commit.
