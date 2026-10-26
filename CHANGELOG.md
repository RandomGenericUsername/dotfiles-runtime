# Changelog

## 0.2.0

The palette is a required artifact (BUG-001).

- `wallpaper set` now prepares the palette (hard) **before** the visible swap:
  a palette failure exits non-zero without swapping the wallpaper, writing
  `current.json`, or changing consumer links. A successful set still swaps and
  persists a real palette.
- `PaletteUnavailableError` carries the wallpaper path + sha256, stage, tool,
  csg return code and stderr tail; the CLI renders it in plain/JSON, emits a
  reasoned `wallpaper.state:error`, and appends a traceable `failures.jsonl`
  record (`inspect failures`).
- Reconcile refuses a legacy null-palette state with
  `PaletteUnavailableError` instead of unlinking consumer pointers; the
  `remove_on_null_palette` seeder rule is removed.
- Progress stages move palette generation ahead of `setting_wallpaper`; the
  `wallpaper.state` order is `generating_palette` → `palette_generated` →
  `setting_wallpaper` → `visible` → `done`.
- AGS standalone fallbacks are unchanged; the composition no longer relies on
  them to mask a missing palette.

## 0.1.0

The runtime becomes its own repo, extracted from `dotfiles-v3` with history preserved.

- `dotfiles-runtime` (import package `runtime`) owns the wallpaper apply/reconcile chain, the
  content-addressed cache, the state/history store, the D-Bus event daemon, the clipboard watcher
  and the `inspect`/`doctor` surfaces.
- `cli-output` and `cli-invocation-contract` are consumed as immutable git tags.
- The event-contract conformance copies and `scripts/contract-pin-check.py` move here; the vendored
  generated binding and embedded state schemas remain package data.
- The `mu` harness, ticket ledger, OpenSpec root and fresh-clone gates are installed from the
  first commit.
- No runtime behaviour change: every adapter, command and artifact contract is the same code that
  shipped inside `dotfiles-v3`.
