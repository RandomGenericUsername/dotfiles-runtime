# Changelog

## 0.2.1

A csg timeout is a bounded, clean failure that cannot discard a complete
palette (BUG-002).

- `CsgAdapter`'s default timeout is 330 s, above csg's 300 s internal
  container timeout, so csg's own `finally` removes the container before the
  host process can be killed.
- On an explicit/forced timeout the adapter SIGINTs the child first (letting
  csg run that cleanup) and only escalates to SIGKILL after a bounded grace.
- Every timeout path verifies the complete 8-artifact set: a complete palette
  is accepted and published instead of being deleted by staging cleanup; an
  incomplete set raises `PaletteUnavailableError` naming the missing/non-file
  artifacts plus elapsed time, termination signal, return code and stderr.
- The real-input proof is success-required for 8K/4K cold/warm, and an opt-in
  forced 2 s timeout asserts the complete palette is kept and no new container
  remains in the pre/post `podman ps -a` diff.
- The 512 MiB csg container limit is kept: measured 8K peak is 310 MiB (190
  MiB at 4K), 512m passes and 256m fails the 8K fixture. The limit is
  csg-owned (`[container].memory_limit`); the runtime does not override it.
- Container lifecycle ownership: adapter/csg timeout cleanup is csg's
  `finally`; stale containers after SIGKILL/power loss need a csg-owned
  label/reaper sweep (recorded as a cross-repo handoff). The runtime still
  only invokes the CLI and never runs containers itself.

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
