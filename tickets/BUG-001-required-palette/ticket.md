---
id: BUG-001
title: "Palette is mandatory: fail before the visible swap and refuse null on reconcile"
kind: bug
status: in-progress
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-001/
branch: ticket/BUG-001-required-palette
repro: "uv run pytest -q tests/integration/test_visible_first_swap_integration.py::TestPostVisibleFailure::test_palette_failure_keeps_wallpaper_with_null_palette tests/unit/test_reconcile.py::test_null_palette_removes_r2_consumer_symlink"
observed: "Both repro tests pass on main: a failed palette is persisted as current.json palette=null after the wallpaper swap, and reconcile then removes every consumer pointer (login-reconcile.log: 'seeding: palette layer is null; consumer symlink removed: .../config/ags/colors.css'); history.jsonl has 25 null-palette lines."
expected: "A failed palette exits non-zero before any swap/state write/consumer-link change, leaves current.json byte-identical, never persists palette=null, records a traceable failure (wallpaper path+sha256, stage=palette, tool=csg, rc, stderr tail) visible to inspect/journal, and reconcile refuses a legacy null-palette state with PaletteUnavailableError instead of unlinking consumers."
---

# Palette is mandatory: fail before the visible swap and refuse null on reconcile

## Triage (`/triage`, 2026-10-26)

- `kind: bug` — a failing palette currently degrades the desktop (null state +
  removed consumer links), which is a behavior defect against the required
  composition contract, not a feature request.
- `lane: 2 — openspec-single (single bounded contract change)` — one
  contract change ("palette is a required artifact") spanning the CLI/apply
  ordering and the reconcile/seeder guard, fully understood from the plan
  (`/home/inumaki/.opencode/plan/01-runtime-BUG-001-required-palette.md`).
  The bug lane closeout is the fix + the flipped regression tests; no separate
  OpenSpec change is opened for this defect.
- `severity: high` — the failure removes the AGS/GTK/rofi/btop palette pointers
  and leaves the desktop unstyled (the wlogout stylesheet is unreadable through
  `~/.config/wlogout/style.css:1`), while the command still leaves a
  null-palette state behind.
- `Evidence:` `src/runtime/application/swap_visible.py:135` (current save with
  `palette=None`); `tests/integration/test_visible_first_swap_integration.py:304-322`
  (the test asserts the degradation); `src/runtime/adapters/seeder.py:772-783`
  (`remove_on_null_palette`); `~/.local/state/dotfiles/login-reconcile.log:116-120`
  (five consumer links removed); history lines 217/223 (2026-10-26 null
  palettes for `windows-95`/`yasuke`).
- `Verify:` `uv run pytest -q tests/integration/test_visible_first_swap_integration.py::TestPostVisibleFailure::test_palette_failure_keeps_wallpaper_with_null_palette tests/unit/test_reconcile.py::test_null_palette_removes_r2_consumer_symlink` → `2 passed` on the pre-fix tree.

## Repro

```bash
# Current degradation contract, encoded as passing tests on main:
uv run pytest -q tests/integration/test_visible_first_swap_integration.py::TestPostVisibleFailure::test_palette_failure_keeps_wallpaper_with_null_palette
uv run pytest -q tests/unit/test_reconcile.py::test_null_palette_removes_r2_consumer_symlink

# Host evidence:
grep -n "palette layer is null" ~/.local/state/dotfiles/login-reconcile.log | head
grep -c "pal=null\|palette.*null" ~/.local/state/dotfiles/history.jsonl
```

## Observed

- `swap_visible.py:135` persists `palette=None` **before** derivation, and the
  integration test explicitly asserts "The new wallpaper stays live with a null
  palette; no history line".
- `ApplyWallpaperUseCase` arrives too late: its palette hard-failure only runs
  after the swap/persist already happened (`apply_wallpaper.py:190-192`).
- Reconcile's seeder branch `remove_on_null_palette` deletes every consumer
  pointer. Host log:
  `seeding: palette layer is null; consumer symlink removed: /home/inumaki/.local/share/dotfiles/config/ags/colors.css`
  (also gtk-3.0, gtk-4.0, rofi, btop).
- `grep -c "pal=null\|palette.*null" ~/.local/state/dotfiles/history.jsonl` → `25`.
- Real inputs that produced null-palette reconciles on 2026-10-26:
  `yasuke-dark-7680x4320-21997.jpg` and
  `windows-95-dark-3840x2160-27097.jpg`.
- OOM is not the cause: kernel OOM only 2026-10-09 (`magick`, manual
  `systemd-run weg batch all`); no OOM lines on 2026-10-25/26.

## Expected

- The palette is a required artifact: `wallpaper set` validates + hashes +
  cache-imports the wallpaper, then runs `ensure_palette` **hard** before any
  visible swap.
- A failed palette:
  - exits non-zero,
  - leaves `current.json` byte-identical,
  - does not repoint/remove any `current/` or spine consumer link,
  - does not apply/reload the wallpaper,
  - emits a `wallpaper.state:error` carrying the reason,
  - appends a traceable failure record and logs the csg invocation at ERROR.
- No `wallpaper set` may persist `palette: null`.
- Legacy null-palette states stay readable by `inspect`, but `reconcile`
  refuses them with `PaletteUnavailableError` naming the source wallpaper and
  touches no consumer links.
- AGS standalone fallbacks stay; the composition never relies on them.

## Fix notes

- Reorder the CLI composition root: prepare/ensure palette (hard) → swap with
  the prepared palette → effects/icons (graceful) → reconcile/reload.
- Add `PaletteUnavailableError` (domain) carrying `wallpaper_path`,
  `wallpaper_hash`, `stage="palette"`, `tool="csg"`, `returncode`, `stderr`,
  hint; `CsgAdapter` populates rc/stderr, the application layer wraps other
  palette-stage failures without losing them.
- Append failures to a runtime-owned `failures.jsonl` (the embedded
  `history.schema.json` has `additionalProperties: false` and a closed trigger
  enum, so `trigger: "set-failed"` cannot go into history without a contracts
  change); expose it via `inspect failures` and the ERROR journal line.
- Delete the `remove_on_null_palette` branch/rule usage from the seeder and
  make `repoint_consumer_symlinks` reject a null palette; add the reconcile
  guard before any state/link mutation.
- `SwapVisibleUseCase` requires a palette, persists it (never `None`), and may
  repoint the palette artifacts alongside the wallpaper; effects/icons remain
  null until reconcile.

## Event-order handoff (docs live in `dotfiles-v3` — do not edit there)

New `wallpaper.state` order for `wallpaper set` (hand to OPS-0113 or a small
v3 docs commit updating `docs/ags-restart-vs-live-theme-refresh.md`):

1. `applying` / `generating_palette` (pending hash)
2. `applying` / `palette_generated` (prepared hash)
3. `applying` / `setting_wallpaper` (before the visible swap)
4. `visible` / `preparing_appearance_assets` (pixels swapped, theming in flight)
5. `visible` / `appearance_assets_ready`
6. `visible` / `reconciling_consumers`
7. `done` / `finished`

On a palette failure only steps 1 and an `error` / `generating_palette` event
(with the pending hash and reason) are emitted; no `visible` is ever published.
The selector's collapse-on-visible behavior is intended and out of scope.

## Acceptance

- [ ] New/flipped regression tests are green: `make test`.
- [ ] `make gates` is green (`test`, `lint`, `contracts-check`, `hermetic`, `lock-check`, `docs-check`).
- [ ] Real-input proof (`make test-integration` with `DOTFILES_REAL_WALLPAPERS`) runs the real `csg` on both fixtures in a scratch state root and reports palette-or-clean-error with csg rc/stderr.
- [ ] Manual host proof: failing palette leaves `current.json`/links unchanged; a successful set still swaps + persists a real palette.
- [ ] Ticket closeout records red/green command output; tag `v0.2.0` only if green.
