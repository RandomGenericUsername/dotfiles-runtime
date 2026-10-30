# Changelog

## 0.4.1

Runtime-owned palette producer config, tpr container runtime, and precise
palette-failure diagnostics (REQ-004, REQ-005, BUG-007).

- The producer is resolved from `<install_spine>/config/dotfiles-runtime/producer.json`
  (schema version 1) with the order explicit `--producer` > config > default `csg` with a
  warning. `RUNTIME__PALETTE__PRODUCER`/`RUNTIME__PALETTE__TPR_ENGINE` are no longer
  authoritative; stale values are reported as `ignored_env` and warned about.
  Malformed config fails loud.
- `dotfiles-runtime inspect producer --format json` reports producer/source/engine/runtime,
  the config path, ambient env, ignored env and warnings. `wallpaper set --format json`
  gains `producer`, `producer_source` and `producer_engine`.
- Every CLI/daemon composition root uses the same operation-time resolver, so a selector
  launched before provisioning cannot pin the old producer.
- `TprAdapter` runs the container runtime by default (`local` remains explicit for tests/dev),
  pins `THEME__TEMPLATES__DIRECTORY` to the hashed spine catalog, and pins the provisioned
  `theme-producer/settings.toml` so the container image tag/engine come from provisioning.
  No host `tpr-native` is required.
- `CsgAdapter` pins `COLORSCHEME_TEMPLATES_TEMPLATES_DIR` to the same templates dir it hashes,
  so a selector CWD cannot select an unrelated catalog.
- Exit-0 runs with missing/partial artifacts now raise typed `PaletteUnavailableError` carrying
  the real return code, stdout, stderr, elapsed time, timeout/termination state and missing
  names. `failures.jsonl` and `inspect failures` carry the optional diagnostics (old records
  remain readable), and failures record the real producer (`tpr` is never labelled `csg`).
- The existing fail-busy lock contract is unchanged: no retry or queue was added.

## 0.4.0

Palette producer seam (REQ-003): the runtime can produce a palette with `tpr` as well as `csg`,
without changing the default.

- `RUNTIME__PALETTE__PRODUCER=csg|tpr` selects the producer; absent/`csg` keeps today's behavior.
- `TprAdapter` invokes the pinned `tpr` contract record (`generate` + eight `--format`s, `THEME__`
  output env), copies exactly the eight palette artifacts, and ignores the canonical `theme.json`.
- CSG palette keys are byte-identical to 0.3.0 (seed `None`). TPR keys add
  `sha256("tpr:<engine>:<version>:<templates_hash>")` as the seed, so a producer switch cannot hit
  the other's cache entry.
- `derive.ensure_palette` and `invalidation.recomputed()` share the producer seed formula and the
  producer-specific templates dir; `THEME__` is added to the subprocess env allowlist.
- `cli-invocation-contract` re-pinned to `v0.4.0` (the `tpr` record).

## 0.3.0

Manifest-driven AGS palette refresh, applied-appearance ledger, parallel live /
serialized restart lanes and clipboard namespace migration (REQ-002), plus
confirmed AGS restart liveness (BUG-006).

- Discovery reads every deployed link under the installed `<install>/config`
  spine and validates `app.manifest.json` identity against `app.tsx` and
  `ags list`; missing/invalid/duplicate manifests and foreign/dev owners are
  structured failures. Restart apps are included even when down; dev instances
  never refresh.
- One applied-appearance ledger under `$XDG_STATE_HOME/dotfiles/` records the
  palette/icons/effects hash and per-app `applied|deferred|failed|skipped`
  results atomically, retries deferred/failed/skipped apps on the next trigger,
  and is serialized by one operation lock.
- Running live apps refresh in parallel via
  `ags request palette-refresh <appearance-hash> -i <instance>` (5 s timeout, one
  JSON string ack); the bar and other restart apps use the serialized BUG-006
  quit/confirm/launch lane. A live failure never restarts an app. Runtime-owned
  per-app JSONL records and structured `reload_failures` carry the outcome.
- AGS restarts now confirm liveness end to end (BUG-006): every `ags run`
  caller shares `$XDG_RUNTIME_DIR/ags-launch.lock`, quit waits up to 5 s for the
  intended D-Bus name (and known pid) to release, a still-owned name fails
  without launching, and exit 0 counts as delegation only when the intended
  instance owns its name afterwards. One wrapper per intended instance.
- Clipboard config/state/cache paths move from `hypr-pano` to `clipboard` with a
  one-time migration of history (favorites/recency), preferences and images.
- `CONTRACT_PIN` moves to contracts `5eb01b8`; the AGS manifest, install-list,
  program-tuple and refresh-result schemas are embedded and conformance-checked.
  The `wallpaper.state` description is explicitly progress/UI only (AD-27).

## 0.2.3

An absent optional intent root is informational, not degraded watch health
(BUG-005).

- The optional `$XDG_CONFIG_HOME/dotfiles/desired.json` root carries explicit
  optional metadata: an absent file or absent parent directory is reported as
  `optional_absent`, logged at INFO, and does not degrade watch health. Only a
  required-root failure sets `failed_roots`/`degraded`.
- `watch-health.json` gains the additive `optional_absent` field under the
  unchanged `version: 1`; old records without the key read as an empty tuple,
  and a malformed value reads as absent (like the other fields).
- `inspect daemon` and `doctor` include `optional_absent` in JSON and print
  `OPTIONAL (absent, not watched) <path>` in plain output while keeping exit 0;
  the degraded/`UNWATCHED` wording remains reserved for required roots.
- `WatchCoordinator` retries once per burst when an optional root is absent, so
  a parent later created by the user is watched without polling or a daemon
  restart. Required-root warning/failure behavior is unchanged.
- Per the AD-39/AD-40 contract, the root set stays exactly the five enumerated
  roots: no parent/ancestor watches, no polling, and no D-Bus/event-contract
  change.

## 0.2.2

The icon-contrast prefs reader accepts its own canonical document at any entry
count (BUG-003).

- `parse` now treats a remainder of only separators/whitespace as valid after
  removing the `"<hex>": true|false` entries, so a canonical version-1
  document with four or more prefs round-trips instead of falling back to
  defaults. Truly malformed documents still return `{}` with a warning.
- Regression coverage pins `0/1/2/3/4/5/10`-entry `parse(serialize(...))`
  round-trips, mixed values, the canonical four-entry shape as an inline
  fixture (not a host read), and `write_pref` ×4/×5/×10 read-back.
- BUG-004's btop-theme-studio skip is retained: a wallpaper change leaves the
  studio running so unsaved edits are not lost. Live refresh and
  pending-aware semantics remain `EPIC-002`/`REQ-010` work, not part of this
  release.
- Includes BUG-002 (csg timeout/cleanup), released in v0.2.1; no further
  changes to it in this release.

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
