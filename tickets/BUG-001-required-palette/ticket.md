---
id: BUG-001
title: "Palette is mandatory: fail before the visible swap and refuse null on reconcile"
kind: bug
status: done
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

- [x] New/flipped regression tests are green: `make test` → `2126 passed, 3 skipped, 20 deselected`.
- [x] `make gates` is green (`test`, `lint`, `contracts-check`, `hermetic`, `lock-check`, `docs-check`).
- [x] Real-input proof (`make test-integration` with `DOTFILES_REAL_WALLPAPERS`) runs the real `csg` on both fixtures in a scratch state root and reports palette-or-clean-error with csg rc/stderr.
- [x] Manual host proof: failing palette leaves `current.json`/links unchanged; a successful set still swaps + persists a real palette (real-filesystem integration).
- [x] Ticket closeout records red/green command output; tag `v0.2.0` only if green.

## Evidence

### Red (before the fix)

| Claim | Command | Result |
| --- | --- | --- |
| Old degradation contract is encoded and passing on main `0bfe1b5` | `uv run pytest -q tests/integration/test_visible_first_swap_integration.py::TestPostVisibleFailure::test_palette_failure_keeps_wallpaper_with_null_palette tests/unit/test_reconcile.py::TestReconcileFailurePolicy::test_null_palette_removes_r2_consumer_symlink` | `2 passed in 0.10s` (exit 0) — the null-palette persist + consumer unlink are the asserted behavior |
| New tests are red on `873edd1` | `uv run pytest -q tests/integration/test_required_palette.py tests/unit/test_inspect_failures.py` | `ImportError: cannot import name 'InspectFailuresUseCase'` / `PaletteUnavailableError` (exit 2) — the required contract is absent |

Full logs: `/tmp/opencode/bug001-old-behavior.txt`, `/tmp/opencode/bug001-red-new.txt`, `/tmp/opencode/bug001-red.txt`.

### Green (after the fix)

| Claim | Command | Result |
| --- | --- | --- |
| Flipped/new targeted suite | `uv run pytest -q tests/unit/test_csg_adapter.py tests/unit/test_reconcile.py tests/unit/test_seed_cache.py tests/unit/test_inspect_failures.py tests/unit/test_cli_wallpaper_set.py tests/unit/test_wallpaper_visible_first.py tests/integration/test_required_palette.py tests/integration/test_visible_first_swap_integration.py` | `166 passed in 0.85s` (exit 0) |
| Fast suite (`make test`) | `make test` | `2126 passed, 3 skipped, 20 deselected in 53.47s` (exit 0) |
| Gates (`make gates`) | `make gates` | exit 0: test `2126 passed, 3 skipped, 20 deselected`; lint `All checks passed!`; contracts-check `39 passed, 1 skipped`; hermetic `2126 passed, 3 skipped, 20 deselected`; lock-check `Resolved 25 packages`; docs-check silent/pass |
| Full suite incl. integration | `make test-integration` | `2143 passed, 5 skipped, 1 xpassed in 104.28s` (exit 0) |
| Real-input proof (8K + 4K, scratch state root) | `DOTFILES_REAL_WALLPAPERS=/home/inumaki/.local/share/dotfiles/wallpapers uv run pytest -q -s tests/integration/test_required_palette_real_inputs.py` | `2 passed in 25.65s`; captured lines below |
| Failure-leaves-state-untouched + success-persists-palette | `uv run pytest -q tests/integration/test_required_palette.py` | included in the 166-pass run: `current.json` byte-identical, `current/` + spine links unchanged, `applier.calls == 0`, one `failures.jsonl` record with rc/stderr, and the success case persists a non-null palette + real cache entry |

Full logs: `/tmp/opencode/bug001-make-gates-final.txt`,
`/tmp/opencode/bug001-make-test-integration-final.txt`,
`/tmp/opencode/bug001-real-inputs-final.txt`,
`/tmp/opencode/bug001-green-targeted.txt`.

### csg real-input capture (separate issue, not modified)

The two fixtures were run through the real `csg` via `CsgAdapter` in a pytest
`tmp_path` scratch state root (never the live state root):

```
REAL-INPUT csg OK fixture=yasuke-dark-7680x4320-21997.jpg rc=0 stderr='' entry=724411382d96fffd366f17366f95d43ad0fbe8c9e74d24a49bf12b6d67557136 cache_hit=False
REAL-INPUT csg OK fixture=windows-95-dark-3840x2160-27097.jpg rc=0 stderr='' entry=908b2601499075ca6fdea033c6e9c78695ea396caf523fe009b634bbd59de8b0 cache_hit=False
2 passed in 25.65s
```

The historical 2026-10-26 null-palette lines are therefore not reproducible via
`csg` on this host today: two direct `csg generate` runs and the runtime adapter
both exited `0` with empty stderr. No `color-scheme-generator` code was touched.
If a future `csg` run does fail, the runtime now hard-fails cleanly with the
exact rc/stderr in the typed error, event, journal and `failures.jsonl`.

`Verify:` `DOTFILES_REAL_WALLPAPERS=$HOME/.local/share/dotfiles/wallpapers uv run pytest -q -s tests/integration/test_required_palette_real_inputs.py`

### Event-order handoff

`wallpaper set` now emits: `applying/generating_palette` →
`applying/palette_generated` → `applying/setting_wallpaper` →
`visible/preparing_appearance_assets` → `visible/appearance_assets_ready` →
`visible/reconciling_consumers` → `done/finished`. On a palette failure only
`applying/generating_palette` then `error/generating_palette` (pending hash +
reason) are emitted; `visible` is never published. The stage-order doc lives in
`dotfiles-v3` (`docs/ags-restart-vs-live-theme-refresh.md`); this ticket is the
handoff (OPS-0113) and that repo was not edited.

### Commits / release

- Planning: `0bfe1b5` (ticket + INDEX)
- Red tests: `873edd1`; alignment `e26479a`
- Fix: `fef40b6`; narrowing/record polish `3e97d35`
- Release prep: `95303de` (`0.2.0` pyproject + lock + CHANGELOG)
- Worktree `.worktrees/BUG-001/`, branch `ticket/BUG-001-required-palette`.
- Merge: fast-forward `0bfe1b5..376be9f` into `main`; local `make gates` green on merged `main`.
- Annotated tag `v0.2.0` → tag object
  `64368a0ebe81d9dab50e57d35ad35dd181e8f725`, commit
  `376be9f7399961e27857e7bc6bb6f974e61faf58`.
- GitHub Release: <https://github.com/RandomGenericUsername/dotfiles-runtime/releases/tag/v0.2.0>
  with `dotfiles_runtime-0.2.0-py3-none-any.whl` +
  `dotfiles_runtime-0.2.0.tar.gz`.
- Tag CI run `40353959361`: all jobs success, including `release`.

`Verify:` `git rev-parse v0.2.0 'v0.2.0^{commit}'`; `gh release view v0.2.0 --json assets --jq '.assets[].name'`.
