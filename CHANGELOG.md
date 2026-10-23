# Changelog

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
