# Changelog

## v1.0.1 — 2026-11-07

Fix the daemon converge job trigger and the `wallpaper.state` contract rejection
(BUG-010).

- The daemon queue no longer hardcodes `trigger="daemon"`. `daemon` is a
  generation-manifest source kind, not a history trigger; the worker preserves
  the request's canonical trigger (`set` for `wallpaper set`, `reconcile` for a
  standalone reconcile) and falls back to `reactive` when the request is
  absent or unknown. `suppressHistory` and `includeTerminal` now travel in the
  converge request, so a reactive composite's inner reconcile stays silent and
  the composite appends exactly one `reactive` history line.
- `wallpaper.state` is a bounded, contract-valid summary: only contract stages
  (`setting_wallpaper`, `generating_palette`, `reconciling_consumers`,
  `finished`), error and reload-failure text collapsed and clamped, and optional
  fields dropped if a future value would push canonical JSON over the hub's
  64 KiB cap.
- A `wallpaper.state` publish failure is a visible warning. It no longer prints
  a traceback or changes an otherwise successful apply/reconcile result.
- Regression tests cover allowed/fallback triggers, daemon `set` and
  `reactive`, the no-daemon CLI fallback, payload size/schema, and non-fatal
  publishing.

## v1.0.0 — 2026-11-06

Generations v3: one committed generation and one `converge(intent)` (REQ-020).

- **Generation model.** `state/generations/<genHash>/` now holds
  `manifest.json` (contracts v0.8.0 generation schema), `palette/`, `effects/`,
  `icons/`, `wallpaper/`, `appearance/<configDir>.css` and
  `appearance/tokens.json`. Layers stay content-addressed and are referenced,
  not copied. Staging is a sibling `generations/.staging-*`; files and
  directories are fsynced and the immutable generation is published with one
  `rename`. `state/current` is one symlink committed with one `rename`; every
  consumer reads through `current/`. Compatibility aliases (`colors.*`,
  `wallpaper*.png`) live *inside* the generation and cannot skew.
- **One convergence function.** Daemon reactions, `reconcile`, `wallpaper set`
  and skin/mode changes all call `ConvergeEngine.converge(intent)`. The scope is
  intent → derive → stage → commit → apply/ledger under one blocking kernel
  `converge.lock`; there is no nested seed/appearance lock and no
  derivation-outside-the-lock split. A mode-only change regenerates the palette
  in every path and records the mode in the generation identity.
- **Daemon queue + CLI fallback.** The daemon owns a converge queue over the
  existing `BeginJob`/`JobStarted`/`JobFinished` surface. Identical intents
  coalesce; the newest queued intent supersedes older queued (not running)
  intents; a running job is never cancelled. The CLI preflights
  `NameHasOwner` once, subscribes to `JobFinished` before submitting, and waits
  on its job id (no state-file polling). With no daemon the same function runs
  under the same flock.
- **Generation-bound refresh.** Live apps receive
  `ags request appearance-refresh <genHash> -i <instance>`. Only an ack whose
  generation equals the request and whose `current` is unchanged is recorded
  `applied`; a stale/boolean ack is deferred/failed and retried next trigger.
  The declared `live|restart` lane is enforced; a failed live refresh never
  becomes a restart. Unit restarts use `systemctl --user restart --wait`.
- **Locality.** Runtime writes are state-only. It stops creating/repairing
  `config/<app>/appearance.css`, v1/v2 skin pointers and any file inside an app
  tag; existing runtime-owned leftovers are pruned. Provisioning-owned legacy
  links keep working through `current/palette/...`.
- **Compatibility/recovery.** `current.json` is a derived compatibility view;
  `current/manifest.json` is authoritative. Crash recovery covers before/after
  each rename; a mutated generation is rebuilt; GC retains
  active/newest/pinned/ledger/acknowledged generations and only sweeps
  unreferenced entries older than grace. `state/gc-pins.json` is retention
  metadata only.
- **Events.** AGS name waits use D-Bus `NameOwnerChanged`; systemd waits use
  `--wait`; launch-lock contention is fail-fast, not a sleep/retry loop.
- **Tests.** New generation/queue/ack/locality/GC/inspection tests plus crash
  injection, concurrent lock serialization, queue coalescing/supersede and a
  source check that the converge core has no sleep polling.


## v0.9.5 — 2026-11-06

Carry the desired palette mode into every palette generation and make it part
of the cache identity (REQ-019).

- `desired.json` gains an optional `mode` of `dark`, `light` or `smart`,
  defaulting to `smart`. `DesiredState` carries it; the strict reader rejects
  any other value and still rejects unknown keys. Every apply/reconcile/derive
  composition root that reads the intent resolves the mode.
- `TprAdapter` validates the mode and passes it to `tpr` as
  `THEME__OUTPUT__DEFAULT_MODE=<mode>` in the child env (argv unchanged).
- The palette entry seed now includes the mode:
  `sha256("tpr:" + engine + ":" + version + ":" + template_set_hash + ":" +
  mode)`, so `dark`/`light`/`smart` cannot collide. **Invalidation:** palettes
  written before v0.9.5 have no mode in their key and become cache misses;
  the next apply regenerates them. Reconcile also treats a mode-aware palette
  whose key no longer matches an explicit intent as stale.
- Appearance identity is still `f(palette, skin, mode)`. The appearance mode
  remains the `mode` emitted by the generated palette (`tpr >= 0.6.0`), so
  `smart` resolves to the producer's chosen dark/light generation.
- Regression tests cover desired-state defaults/validation, the adapter env,
  per-mode key stability/difference, smart/light pass-through, intent wiring in
  reconcile/apply, mode-driven reconcile regeneration and cache hit/miss.
- Tests now isolate `XDG_CONFIG_HOME` per test so a live host `desired.json`
  cannot leak into CLI composition tests.

## v0.9.4 — 2026-11-06

Follow-up to v0.9.3 for the live spine shape: `<install>/config/<configDir>`
is a symlink to the immutable app install, so the v1 prune and the doctor v1
check must follow the config-dir symlink (but never the retired skin link).
Without this, the live `config/ags/skin.{css,json}` and
`config/ags-notifications/skin.{css,json}` links would remain after the
cutover.

- `CacheSeeder.prune_v1_skin_pointers` follows a deployed config-dir symlink
  and removes runtime-owned v1 `skin.css`/`skin.json` links inside the app
  install; it still leaves `appearance.css`, palette links, regular files and
  user symlinks untouched.
- `doctor` scans the same symlinked config dirs, so a leftover v1 link is real
  drift instead of being silently skipped.
- Regression test pins pruning through a symlinked config dir.

## v0.9.3 — 2026-11-06

Make the AGS restart lane systemd-aware and finish the v2 pointer cutover
(REQ-018, folding REQ-015). The v2 acceptance test caught a live split-brain:
a skin switch quit the unit-managed bar and relaunched it with a raw
`ags run`, leaving `dotfiles-ags-bar.service` inactive and an orphan bar
outside the unit.

- `adapters/ags_systemd_units.py` discovers `systemctl --user` units matching
  `dotfiles-ags-*.service`, reads each `ExecStart`, and parses the
  `ags run -d <spine>/config/<configDir>` target. A unit is matched to the
  consumed deployed configDir by resolved path, then by basename. No AGS app
  name is hardcoded.
- `AgsReloader.restart_deployed_app` restarts a unit-managed app only with
  `systemctl --user restart <unit>`, one unit at a time, then waits for the
  instance name through `ags list`. It never raw-`ags quit`/`ags run`s that
  app and never falls back to a raw launch when systemd fails. Non-unit apps
  keep the existing quit/release/launch path. `reload()` uses the same rule
  for the bar and does not restart a unit-managed app twice.
- The refresh record and ledger carry the exact systemd argv plus
  stderr on failure, so `doctor` supervision reports the unit inactive and/or
  the stale appearance instead of silently accepting an orphan.
- The v1 skin pointer family is retired: no runtime path publishes
  `current/skins/`, `current/skin.json`, `config/<configDir>/skin.css` or
  `config/<configDir>/skin.json`. Existing runtime-owned config links are
  pruned; appearance and palette pointers, regular files and user symlinks are
  untouched. Reconcile also clears the stale v1 `current/` mirrors.
- `doctor` no longer reports the expected v1/v2 skew on a clean v2 host; a
  runtime-owned v1 link that reappears is real drift.
- Tests use fake systemctl/units for ExecStart parsing, activation, unit-only
  restart, non-unit fallback, restart failure and no-double-restart, plus v1
  pruning safety and a supervision-clean/no-skew doctor assertion.

## v0.9.2 — 2026-11-06

Report and safely repair the always-on AGS `systemd --user` supervision
(REQ-017). Provisioning v0.1.37 owns the static units; this is the RFC §4.3
runtime doctor leg and the RFC §4.2 repair rule.

- `doctor` checks `systemctl --user is-active` for
  `dotfiles-ags-bar.service` (instance `ags`) and
  `dotfiles-ags-notifications.service` (instance `notifications`), reads
  `MainPID`, verifies it is alive, and resolves the instance's AGS D-Bus owner
  PID with `busctl --user status io.Astal.<instance>` cross-checked against
  `ags list`.
- `MainPID` is the foreground `ags run` wrapper; its `gjs` child owns the bus
  name. An owner inside the unit cgroup is healthy even when it differs from
  `MainPID`; an owner outside the cgroup, a dead `MainPID`, or an inactive unit
  is drift.
- Orphan detection reports `ags run` wrappers for an always-on configDir
  outside the unit cgroup, plus their `gjs` descendants. The check never
  signals them.
- The appearance ledger adds a note when a running app was last applied at an
  older appearance hash than the current generation. An absent ledger is
  reported as unknown, never fabricated.
- Absent systemd or a user session is an explicit skip note; it does not change
  the doctor exit code.
- `doctor --repair` is limited to `systemctl --user restart <unit>` and
  `ags quit -i <instance>`; it never kills, `pkill`s or process-tree-kills the
  `ags run` parent. Pure supervision drift is repaired without a reconcile;
  mixed drift performs the safe actions before the existing reconcile.
- `inspect status` remains the read-only state projection; live systemd
  supervision stays in the doctor so there is one live supervision surface.
- Tests inject fake systemctl/ags/busctl/proc seams and cover active, inactive,
  PID mismatch, orphan, stale appearance, absent-systemd skip and exactly the
  allowed repair action set.

## v0.9.1 — 2026-11-06

Consume the real `dotfiles-assets v0.12.0` / `contracts v0.7.0` v2 appearance
manifests (REQ-016). v0.9.0 rejected every real v2 TOML manifest with
`ValueError: skin manifest has unknown keys ['groups', 'schemaVersion']`.

- `SkinExpander` reads `schemaVersion = 2` manifests: nested
  `[groups.surface.{panel,card,cell,control,pill}]`, `[groups.state.*]`,
  `[groups.type]`, `[groups.ink]`, `[groups.layout.<app>]` and optional
  `[mode.dark]`/`[mode.light]` complete same-shape overrides. The authored
  document is validated against the vendored `appearance-manifest` schema;
  structure/variant/literal-hex/transition/unit-string inputs fail closed. The
  v1 `schema_major = 1` path and `skin.json` translation remain for the assets
  tuple bump.
- v2 templates use flattened placeholders (full nested path uppercased with
  non-alphanumerics replaced by `_`: `surface.cell.hover-bg` →
  `{{SKIN_SURFACE_CELL_HOVER_BG}}`). Lengths render `<n>px`,
  `letter-spacing` renders `<n>em`, `font-weight` stays unitless. Unknown,
  unresolved, layout and conditional placeholders fail closed.
- Appearance generation reads v2 TOML, resolves the palette mode, replaces
  each authored override group as a whole, writes the top-level
  `{version: 2, surface, state, type, ink, layout}` `tokens.json` (validated
  against `appearance-json`) and expands the mode-resolved templates before
  composing the sheet. The skin cache stays palette/mode-independent and its
  identity remains `sha256(templates_hash ‖ manifest_hash)`; the appearance
  identity remains `f(palette, skin, mode)`.
- The current `theme-producer` emits no mode field in `colors.yaml`, so the
  runtime still defaults to `dark`. The gap is documented: the producer must
  eventually emit the selected mode; an authored `mode.light` override works
  today.
- Tests expand the real v2 `glass`/`flat`/`paper` manifests × both modes,
  validate the resolved documents, prove the flattened placeholder/unit rules,
  whole-group override completeness, v1 translation, cache hashes, the pointer
  and the composed palette/role/skin CSS blocks.

## v0.9.0 — 2026-11-06

Publish one composed appearance generation behind one atomically repointed
pointer (REQ-014). This adopts the frozen `contracts` v0.7.0 appearance plane
while keeping the existing palette and skin caches independent.

- Generate `cache/appearance/<appearanceHash>/` with `colors.yaml`,
  mode-resolved v2 `tokens.json`, `appearance.json` (`version: 2`,
  `appearanceHash`, `paletteHash`, `skinHash`, `generationHash`, `skin`,
  `mode`, `tokenRef`) and one `<configDir>.css` per skin target. The composed
  sheet is the palette legacy CSS + rendered `@define-color` role block +
  expanded skin CSS.
- `appearanceHash = sha256("appearance.v2\n" + paletteHash + "\n" + skinHash +
  "\n" + mode)`. A wallpaper change reuses the skin expansion; a skin change
  reuses the palette. The appearance ledger/refresh uses this identity; the
  `palette-refresh` transport is unchanged (whole-appearance semantics).
- Mode is derived from the generated palette and is part of the identity. A
  `mode`/`scheme`/`appearance`/`variant` scalar or `is_dark`/`dark` boolean
  selects it; when the producer emits no mode field the generation defaults to
  `dark`. A v2 manifest's `mode.dark`/`mode.light` override replaces its
  same-shape group before `tokens.json`/CSS are written. A v1 `skin.json` is
  translated through the RFC §5.2 compatibility mapping for the migration
  window.
- `current/appearance` is the only repointed hop (temp symlink +
  `os.replace`). Stable `config/<configDir>/appearance.css` links point into
  it; absent config dirs are skipped and runtime-owned stale links are pruned.
  v1 `current/skins/`, `current/skin.json` and `config/<configDir>/skin.*`
  pointers are retained so v1 apps keep working; removal is
  `REQ-015-appearance-v1-pointer-removal`.
- Reconcile/seed publish in the same atomic flow and prune old generations
  under keep-N + active + grace. `inspect status` and `doctor` report
  appearance hash/skin/mode/pointer and watch health; doctor flags the v1/v2
  skew while the migration pointers exist.
- Vendored the three v0.7.0 appearance schemas; `tokens.json` and
  `appearance.json` are validated before publication.

## v0.8.2 — 2026-11-06

Emit skin consumer pointers for every skin target, not just AGS (REQ-013).
This corrects v0.7.0–v0.8.1, whose static consumer table hardcoded
`config/ags/skin.{css,json}` while the runtime already emitted
`current/skins/<configDir>.css` for all seven targets.

- Remove the hardcoded AGS skin pair from `StaticConsumerPathSpec`. The
  fixed table keeps only the palette/theme consumers (`ags`, `gtk-3.0`,
  `gtk-4.0`, `rofi`, `btop`).
- Derive skin pointers from the active `SkinEntry` artifacts: for every
  `<configDir>.css`, ensure `<install>/config/<configDir>/skin.css` →
  `current/skins/<configDir>.css` and
  `<install>/config/<configDir>/skin.json` → `current/skin.json`. Config
  dirs are data-derived, so all seven AGS apps (and future targets) work
  without a runtime change.
- Only existing app config dirs are touched; an absent dir is a DEBUG
  skip, never an error and never an mkdir into the install spine.
- Reconcile prunes stale runtime-owned skin pointers when a generation
  drops a target. The pinned palette/theme pointers and user-owned files
  are never removed. Repoints remain atomic (`tmp` symlink + `os.replace`)
  and idempotent.
- `inspect status` and `doctor` project the derived skin pointer set for
  installed apps and do not report absent app dirs as drift.

## v0.8.1 — 2026-11-05

Accept the skins v0.6.0 `shape.hairline-w` token (REQ-012). This corrects the
v0.8.0 vocabulary, which still matched the 23-token v0.5.2 contract and
therefore rejected every real assets manifest at the integration gate.

- `hairline-w` joins the `shape` group and the numeric vocabulary (24 tokens
  total). It is a unitless number; a unit string such as `"1px"` fails closed.
- `border-w` remains the outer panel/frame border; `hairline-w` is the thinner
  card, row-action and badge hairline. Layout tokens remain TSX-owned and the
  runtime rendering path is unchanged.
- Fixtures are the real `glass` (`hairline-w = 1`), `flat` (`0`) and `paper`
  (`1`) manifests; tests assert the `1px` CSS rendering and the raw number in
  `skin.json`.

## v0.8.0 — 2026-11-05

Reconcile the skin expander to the canonical skin interface (REQ-011). This
corrects the v0.7.0 manifest reader, which invented a `version` / `[tokens]` /
`[apps]` shape and rejected every real assets manifest.

- Manifests are `schema_major = 1`, `name`, and exactly the six groups
  `[shape]`, `[surface]`, `[ink]`, `[type]`, `[rhythm]`, `[layout]`; there is no
  `[tokens]`/`[apps]` table and no structure/variant key. Dimensions must be
  unitless numbers (`radius-panel = 12`, `border-w = 1.5`); a unit string such
  as `"12px"` fails closed.
- Templates are discovered by `<configDir>.gtk.css.j2` and rendered to
  `<configDir>.css`; `{{SKIN_<TOKEN>}}` is the token name uppercased with
  non-alphanumerics replaced by `_`. Unknown/unresolved placeholders fail
  closed.
- Numeric tokens render as `<number>px` in CSS (including layout tokens);
  `cell-pad` and string tokens render verbatim. `skin.json` is the grouped
  `{"version": 1, "<group>": {...}}` map with raw numbers, matching
  `ags-platform` `parseSkinJson`.
- Manifest discovery accepts the canonical `skins/manifests/<name>.toml|json`
  layout on the installed spine. Cache identity, pointers, palette-refresh/
  restart lanes, fail-closed fallback and inspect/doctor/prune are unchanged.

## v0.7.0 — 2026-11-05

Skins derivation layer: palette-independent per-app CSS + resolved token map
(REQ-010).

- `desired.json` gains an optional `skin: "<name>"` (default `glass`); the v1
  assets tag supplies `glass`, `flat` and `paper` manifests.
- Initial `SkinExpander` (superseded in v0.8.0: it expected a non-canonical
  `version` / `tokens` / `apps` manifest and did not match the frozen assets).
- Cache key is `sha256(templates_hash ‖ manifest_hash)` under
  `cache/skins/<sha256>/` with a `meta.json` (`kind: "skin"`). Wallpaper and
  palette changes are cache hits; template/manifest changes are misses.
- `current/skins/<configDir>.css` and `current/skin.json` are atomic symlinks; AGS
  gains the stable consumer pointers `config/ags/skin.css` and
  `config/ags/skin.json`.
- `current.json` carries an optional `skin` projection (`hash`, `name`,
  `artifacts`, `generated_at`); history lines may carry the skin hash.
- `appearance_hash` includes the skin entry hash, so skin changes refresh live
  apps through the existing `palette-refresh` lane; the restart-only bar lane
  is unchanged.
- Failure is fail-closed per skin: missing/invalid assets keep the previous
  skin, log a structured error and never touch app CSS.
- `inspect status` prints the resolved skin + token map; doctor, cache verify
  and prune cover the new `skins` layer. Embedded and canonical schemas stay
  byte-identical (`current`, `meta`, `history`).

## v0.6.1 — 2026-11-05

Release metadata fix: `v0.6.0` shipped `pyproject.toml` and
`runtime.__version__` at `0.5.2`, so `dotfiles-runtime version` and the
provisioning verify check disagreed with the tag.

- Bump `pyproject.toml`, `uv.lock` and `runtime.__version__` to `0.6.1`.
- No behaviour change: the `monitors list` command, schema and filtering are
  identical to `v0.6.0`.

## v0.6.0 — 2026-11-04

Read-only shared monitor catalog: `dotfiles-runtime monitors list --format json`
(REQ-009).

- Adds the `IMonitorCatalog` sibling port; `HyprlandMonitorSource` implements
  both it and `IMonitorSource`. `detect_monitors()` behavior is unchanged.
- Emits `schema_version: 1` with `connector`, `label`, `description`, `make`,
  `model`, `serial`, `geometry`, `scale`, `transform`, `focused` and
  `mirror_of`, filtered to connected, enabled, non-mirrored outputs and ordered
  by `geometry.x`, `geometry.y`, `connector`.
- Headless/unavailable exits 0 with `source: "unavailable"` and `monitors: []`.
- Adds the runtime-owned schema `contracts/schemas/monitors-list.schema.json`
  (no `contracts` repo or program-tuple bump). No daemon, no state write, no
  XDG write.

## 0.5.2

Bar palette refresh: a bare `ags run` is attributed to its effective default
config dir so deployed-link discovery can restart the bar (BUG-008).

- `_discover_ags_apps` now records a bare `ags run` (the bar autostart) using
  `$XDG_CONFIG_HOME/ags` from the process environment, falling back to
  `$HOME/.config/ags` and then this runtime's `~/.config/ags`. The instance
  name comes from the config dir's `app.tsx`, else `ags`.
- Ownership stays fail-closed: discovery still resolves the attributed config
  dir and only accepts it when it matches the deployed link. A bare process
  with a different config dir is still reported as foreign/outside.
- `AgsReloader.reload()` skips the discovered bar instance because the bar is
  restarted unconditionally; no duplicate quit/launch cycle.
- Explicit `-d/--directory` discovery is unchanged.

## 0.5.1

Seam cleanup: delete the icon-contrast guard and retire the CSG producer
(REQ-007). No compatibility layer remains.

- Icons render exactly what the icons manifest resolves. The WCAG guard, the
  staging overlay/retargeting path, the per-wallpaper preference store, the
  `--contrast auto|on|off` flags on `wallpaper set` / `icons regenerate`, the
  `icons preference` command, and the icons `meta.json` `contrast` block are
  removed.
- `csg` is no longer a palette producer. `producer.json` still carries the
  seam but accepts only the registered, vocabulary-conformant `tpr` producer;
  an unknown producer (including `csg`) raises a typed
  `UnsupportedPaletteProducerError` naming the supported set. Ambient
  `RUNTIME__PALETTE__PRODUCER` / `RUNTIME__PALETTE__TPR_ENGINE` remain ignored
  and reported.
- The `palette.v1` conformance gate stays fail-closed before staging and
  before the visible swap. A legacy non-v1 palette is reported nonconforming
  instead of "n/a"; the watch-root set follows the theme-producer templates.
- Shared producer subprocess/artifact helpers moved to
  `adapters/producer_process.py`; monitor `bar_backdrop` sampling moved to
  `adapters/wallpaper_luminance.py` (guard-free).

## 0.5.0

Palette vocabulary contract (`palette.v1`) with fail-closed conformance
validation and probe fixtures (REQ-006).

- Declares the 51-role `palette.v1` vocabulary: the 50 theme-producer Material
  roles plus the produced `on_backdrop` role (tpr v0.5.0 / REQ-022).
- Validates every tpr palette (`colors.yaml` top-level roles and
  `colors.gtk.css` `@define-color` definitions) before staging and before the
  visible swap; missing or malformed roles fail closed with a typed error naming
  the roles. Extra roles are additive and reported as info only.
- `inspect palette` and `doctor` report the vocabulary version, conformance and
  extras. A readable producer shape that is not tpr is reported as not
  applicable until the CSG retirement lands.

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
