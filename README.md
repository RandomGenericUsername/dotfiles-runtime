# dotfiles-runtime

The host-side runtime for the dotfiles desktop: wallpaper apply → derive → reconcile, the
content-addressed cache, the state/history store, the D-Bus daemon and the clipboard watcher.

This is the program the composition installs (`dotfiles-v3` → `dotfiles/provisioning/cli-tools.yaml`)
and the systemd user units run. It is **not** the installer, the asset corpus, the GUI, or a CLI
tool: it consumes those by pin and drives `tpr` / `weg` / `itr` as subprocesses.

## Public surface

```console
dotfiles-runtime wallpaper set IMAGE      # converge(intent): derive → commit generation → apply
dotfiles-runtime reconcile                # converge(intent) from desired.json / current state
dotfiles-runtime doctor [--repair]        # state/cache and AGS supervision drift
dotfiles-runtime capture                  # resident capture job (hub Control)
dotfiles-runtime clipboard                # resident clipboard watcher
dotfiles-runtime icons regenerate         # re-render the icon set
dotfiles-runtime gtk4 restart             # reload GTK4 subscribers
dotfiles-runtime daemon run               # systemd ExecStart: org.dotfiles.Events owner
dotfiles-runtime inspect status|daemon|history|failures|producer
dotfiles-runtime inspect cache list|prune
dotfiles-runtime monitors list [--format json]  # read-only monitor catalog
dotfiles-runtime version
```

Global flag: `--format {json,plain,rich}` (via `cli-output`).

## Release notes

**1.0.8** — stable appearance links are not v2 drift.

- `doctor` now accepts the provisioning-owned
  `config/<configDir>/appearance.css` link when it names exactly
  `<stateRoot>/current/appearance/<configDir>.css` and the target exists.
  Generation paths, app tags, old v2 targets, wrong basenames and dangling
  links still report retired drift. The live wlogout link is clean, and the
  AGS app-symlink skip plus the `skin.css`/`skin.json` checks are unchanged.

**1.0.7** — wlogout skin target regression proof.

- The released `wlogout.gtk.css.j2` from `dotfiles-assets v0.14.0` is now a
  hermetic v2 fixture. The existing `<configDir>.gtk.css.j2` derivation already
  yields `wlogout` in `config_dirs`, `appearance/wlogout.css` in the appearance
  entry and generation, and the canonical manifest target; the new tests pin
  that behaviour and validate the committed manifest against the embedded
  contracts v0.8.0 generation schema. No production code changed.

**1.0.6** — one state-mutation lock.

- `state/converge.lock` is now the only lock file runtime commands create.
  First-run seeding is a sub-step of that lock with a pass-through inner
  mutex, and `icons regenerate` commits a generation through the same scope
  (non-blocking, preserving the GUI busy/error contract). The legacy
  `.seed.lock` is gone.

**1.0.1** — valid daemon converge triggers and a contract-safe `wallpaper.state`.

- Daemon queue jobs preserve the originating intent trigger (`set` for
  `wallpaper set`, `reconcile` for a standalone reconcile) and fall back to
  `reactive`; the queue no longer writes the invalid `daemon` trigger. A
  reactive composite's inner reconcile stays history-suppressed through the
  daemon boundary, so it appends exactly one `reactive` line.
- `wallpaper.state` uses only contract stages, clamps optional error and
  reload-failure text, and stays below the hub's 64 KiB payload cap. A publish
  failure is a warning and never masks the apply/reconcile result.

**1.0.0** — generations v3: one committed generation and one `converge(intent)`.

- The state plane is `state/generations/<genHash>/` plus one atomic
  `state/current -> generations/<genHash>` symlink. A generation contains
  `manifest.json`, `palette/`, `effects/`, `icons/`, `wallpaper/`,
  `appearance/<configDir>.css` and `appearance/tokens.json`. Layer entries are
  content-addressed and referenced; `manifest.json` is written last.
- Every mutation (daemon watch, `reconcile`, `wallpaper set`, skin/mode change)
  calls the same `converge(intent)` under one blocking `converge.lock`. A mode
  change regenerates the palette in every path.
- The daemon queue coalesces identical intents and supersedes queued ones; the
  CLI subscribes to `JobFinished` before submitting and never polls state
  files. Without a daemon the same function runs under the same flock.
- Live refresh is generation-bound: `ags request appearance-refresh <genHash>
  -i <instance>`. A stale/boolean ack is never `applied`; the declared
  `live|restart` lane is enforced and a failed live refresh never restarts.
- Runtime writes are state-only. It no longer creates
  `config/<app>/appearance.css` or v1/v2 skin pointers and never writes into
  an app tag; provisioning-owned links resolve through `current/palette/...`.
- `current.json` is a derived compatibility view. `current/manifest.json` is
  authoritative; crash recovery, GC (`gc-pins.json`) and `doctor`/`inspect`
  report desired vs committed vs applied.
- AGS readiness uses D-Bus `NameOwnerChanged`; a systemd restart uses the
  blocking `systemctl --user restart <unit>` job (never `--wait`, which waits
  for the started unit to stop again) with a configurable 90 s restart bound
  and a 30 s D-Bus readiness wait; the converge and launch locks have no retry
  sleep loops.

**0.9.5** — palette mode intent from `desired.json` through to `tpr`.

- `desired.json` accepts an optional `"mode": "dark" | "light" | "smart"`
  (default `smart`, meaning the producer picks dark/light from the wallpaper).
  An absent mode keeps the runtime default; any other value is rejected and
  unknown keys still fail the strict reader.
- `tpr` receives the selected mode as `THEME__OUTPUT__DEFAULT_MODE` on its
  settings env, alongside the existing `THEME__*` overrides. argv is unchanged.
- The mode is folded into the palette entry key:
  `sha256("tpr:" + engine + ":" + version + ":" + template_set_hash + ":" +
  mode)`. `dark`, `light` and `smart` can never share a palette entry. The
  one-time consequence is a palette cache miss for pre-mode entries; the next
  apply (or a mode-aware reconcile) regenerates it.
- Appearance identity is unchanged: `appearanceHash = f(palette, skin, mode)`,
  and the appearance mode continues to come from the `mode` emitted in the
  palette's `colors.yaml`.
- Change the mode by editing `desired.json` and running
  `dotfiles-runtime reconcile`; to force a fresh palette explicitly, run
  `dotfiles-runtime wallpaper set "$(jq -r .wallpaper ~/.config/dotfiles/desired.json)"`.

**0.9.4** — follow-up for the live symlinked spine config dirs.

- The live spine exposes `<install>/config/<configDir>` as a symlink to the
  app install. The v1 prune and the `doctor` v1 check now follow that
  config-dir symlink (never the retired skin link), so existing
  `config/<configDir>/skin.{css,json}` links are actually removed after the
  cutover. `appearance.css`, palette links, regular files and user symlinks
  remain untouched.

**0.9.3** — systemd-aware AGS restart and the final v1 pointer cutover.

- The manifest-driven restart lane discovers `systemctl --user` units matching
  `dotfiles-ags-*.service` and parses each `ExecStart`'s
  `ags run -d <spine>/config/<configDir>` target. Discovery is data-driven; no
  AGS app names are hardcoded.
- A unit-managed app restarts only through
  `systemctl --user restart <unit>` (one unit at a time, then the instance
  name is verified through `ags list`). The lane never raw-`ags quit`/`ags run`s
  a unit-managed app and never falls back to a raw launch when systemd fails;
  the exact failed command and stderr land in the refresh record/ledger and
  therefore in `doctor` supervision. A non-unit app keeps the existing
  serialized quit/confirm/launch path.
- The v2 appearance cutover is complete: the runtime stops publishing
  `current/skins/`, `current/skin.json`,
  `config/<configDir>/skin.css` and `config/<configDir>/skin.json`. Existing
  runtime-owned v1 links are pruned; `appearance.css`, palette pointers,
  regular files and user symlinks are untouched. `doctor` reports no v1/v2
  skew on a clean host and treats a reappearing runtime-owned v1 link as real
  drift.

**0.9.2** — doctor supervises the always-on AGS units.

- `doctor` now reports the two `systemd --user` units
  (`dotfiles-ags-bar.service` / instance `ags`, and
  `dotfiles-ags-notifications.service` / instance `notifications`):
  `is-active`, `MainPID` liveness, the AGS D-Bus owner PID
  (`busctl --user status io.Astal.<instance>` cross-checked with `ags list`),
  orphan `ags run`/`gjs` processes outside the unit cgroup, and a ledger note
  when the running app predates the current appearance hash.
- A live D-Bus owner may differ from `MainPID`: the `ags run` wrapper is
  `MainPID` and its `gjs` child owns the name, so an owner inside the unit
  cgroup is healthy. An owner outside the cgroup is drift.
- Absent systemd or a user session is an explicit skip note, never a failure.
- `doctor --repair` is limited to `systemctl --user restart <unit>` and
  `ags quit -i <instance>`. It never kills, `pkill`s or process-tree-kills an
  `ags run` parent (RFC §4.2). Pure supervision drift is repaired without a
  reconcile; mixed drift runs the safe actions before the existing reconcile.

**0.9.1** — consume the real v2 appearance manifests.

- `SkinExpander` accepts `contracts v0.7.0` v2 manifests (`schemaVersion = 2`)
  with nested `groups.surface.*`, `groups.state.*`, `groups.type`, `groups.ink`
  and `groups.layout.<app>`, plus optional `mode.dark`/`mode.light` overrides.
  The document is validated against the vendored `appearance-manifest` schema;
  structure/variant/literal-hex/transition/unit-string inputs fail closed. The
  frozen v1 (`schema_major = 1`) path and its `skin.json` translation still
  work during the assets tuple bump.
- v2 placeholders are the flattened nested token path
  (`surface.panel.bg` → `{{SKIN_SURFACE_PANEL_BG}}`). Lengths render
  `<n>px`, `letter-spacing` renders `<n>em`, `font-weight` stays unitless;
  layout placeholders, conditionals and unknown/unresolved placeholders fail.
- Appearance generation reads v2 TOML directly, applies the selected
  `mode.dark`/`mode.light` override as a whole group before writing
  `tokens.json` or expanding CSS, and composes the same palette legacy CSS +
  role block + mode-resolved skin CSS. The single-hop `current/appearance`
  pointer, convenience links and v1 pointers are unchanged.
- Palette mode detection still uses `mode`/`scheme`/`appearance`/`variant` or
  `is_dark`/`dark`; the current `theme-producer` emits no mode field, so the
  fallback stays `dark`. That producer gap is documented: it must eventually
  emit the selected mode.

**0.9.0** — one appearance generation, one atomically repointed pointer.

- The runtime publishes `cache/appearance/<appearanceHash>/` containing
  `colors.yaml`, the mode-resolved v2 `tokens.json`, `appearance.json`
  (`version: 2`) and one composed `<configDir>.css` per skin target. The
  composed sheet is the palette legacy CSS + rendered `@define-color` role
  block + expanded skin CSS, so an app installs one provider.
- `appearanceHash = sha256("appearance.v2\n" + paletteHash + "\n" + skinHash +
  "\n" + mode)`. Palette and skin caches stay independent: a wallpaper change
  reuses the skin expansion, a skin change reuses the palette. Mode is part of
  the identity; it is read from the generated palette and defaults to `dark`
  when the producer emits no mode field.
- `current/appearance` is the ONE atomically repointed hop. Stable
  `config/<configDir>/appearance.css` links point into it for existing app
  config dirs. v1 `current/skins/`, `current/skin.json` and
  `config/<configDir>/skin.{css,json}` pointers remain during the migration
  window so v1 apps keep working; their removal is
  `REQ-015-appearance-v1-pointer-removal`.
- Reconcile/seed write the generation in the same flow, prune old generations
  (keep-N + active + grace) and stale runtime-owned convenience links.
  `inspect status` / `doctor` report appearance hash, skin, mode, pointer,
  watch health and the v1/v2 skew.

**0.8.2** — skin pointers for every skin target, derived from the generation.

- The static consumer table no longer hardcodes AGS skin entries. For every
  `<configDir>.css` artifact in the active skin, the runtime points
  `<install>/config/<configDir>/skin.css` at `current/skins/<configDir>.css`
  and `<install>/config/<configDir>/skin.json` at `current/skin.json` — only
  when the app config dir exists. The runtime never creates an app config dir.
- Config dirs come from the artifact set, so all seven AGS targets (bar,
  clipboard, notifications, wallpaper selector, capture, icme, btop-studio)
  and any future target get pointers without a code change.
- Reconcile prunes stale runtime-owned skin pointers when a generation drops a
  target; the pinned palette/theme pointers are never pruned. `inspect status`
  and `doctor` report the derived pointer set and omit uninstalled app dirs.

**0.8.1** — accept the skins v0.6.0 `shape.hairline-w` token.

- The shape group and numeric vocabulary gain `hairline-w` (24 tokens total).
  `border-w` stays the outer panel/frame border; `hairline-w` is the thinner
  card, row-action and badge hairline. Layout tokens stay TSX-owned; runtime
  rendering is unchanged.
- The expander fixtures are the real `glass` (`hairline-w = 1`), `flat` (`0`)
  and `paper` (`1`) manifests, covering the raw number in `skin.json` and the
  `1px` CSS rendering.

**0.8.0** — canonical skin interface: grouped manifests, `*.gtk.css.j2` templates, raw-number token map.

- The expander consumes the real assets shape: `schema_major = 1`, `name`, and the six
  `shape`/`surface`/`ink`/`type`/`rhythm`/`layout` groups (no `[tokens]`/`[apps]`).
  Dimensions must be unitless numbers; unit strings fail closed.
- Every `<configDir>.gtk.css.j2` under the shared templates dir renders
  `<configDir>.css`; numeric tokens become `<number>px`, `cell-pad`/strings stay verbatim.
- `skin.json` is the grouped `{"version": 1, "<group>": {...}}` map with raw numbers, so
  `ags-platform` `parseSkinJson` accepts it. Pointers, cache identity and fallback are unchanged.

**0.7.0** — skins derivation layer: palette-independent per-app CSS + token map.

- `desired.json` accepts an optional `"skin": "<name>"` (default `glass`); v1 ships the
  `glass`/`flat`/`paper` manifests in the pinned assets tag.
- A skin is a palette/ink mapping + paint + rhythm only. The runtime expands the assets
  template set with deterministic `{{SKIN_*}}` substitution and caches it under
  `cache/skins/<sha256>/` keyed by `sha256(templates_hash ‖ manifest_hash)`, so a wallpaper or
  palette change never re-expands CSS.
- `current/skins/<configDir>.css` + `current/skin.json` are atomic pointers; AGS gets
  `config/ags/skin.css` and `config/ags/skin.json`. The appearance hash includes the skin, so live
  apps refresh through the existing `palette-refresh` lane (the restart-only bar is unchanged).
- A missing/invalid manifest or template keeps the previous skin, logs a structured error, and
  never touches app CSS. `current.json` carries `{hash, name, artifacts, generated_at}`.
- `inspect status` prints the resolved skin and token map; doctor/verify/prune cover the new
  `skins` cache layer.

**0.5.1** — seam cleanup: the icon-contrast guard and the CSG producer are retired.

- Icons render exactly what the manifest resolves; no runtime retargeting, no
  per-wallpaper contrast store, and no `--contrast` flag.
- `producer.json` accepts only the registered, vocabulary-conformant `tpr`
  producer. `csg` and other unknown values fail with a typed error naming the
  supported set; the ambient `RUNTIME__PALETTE__*` variables remain ignored and
  reported.

**0.4.1** — runtime-owned producer config, tpr container runtime, and palette-failure
diagnostics.

- The palette producer comes from
  `<install_spine>/config/dotfiles-runtime/producer.json`; `--producer` is a one-shot
  override and stale `RUNTIME__PALETTE__*` env is reported, never authoritative. Inspect it
  with `dotfiles-runtime inspect producer --format json`.
- `tpr` runs in its container runtime by default with the spine templates and provisioned
  settings pinned; no host `tpr-native` is required.
- The producer pins the same templates dir it hashes, and incomplete exit-0 runs carry its
  rc/stdout/stderr/elapsed/timeout/missing-artifact diagnostics into `failures.jsonl`.

**0.3.0** — manifest-driven AGS refresh plus confirmed restart liveness.

- Discovery reads every deployed link under `<install>/config`, validates
  `app.manifest.json` against `app.tsx` and `ags list`, and reports
  missing/duplicate/invalid manifests as structured failures. Restart apps are
  included even when down; dev instances are never refreshed.
- The appearance ledger at
  `$XDG_STATE_HOME/dotfiles/appearance-ledger.json` records the applied
  palette/icons/effects hash and per-app `applied|deferred|failed|skipped`
  results atomically, retries non-successful apps on the next trigger, and
  serializes refresh runs under one operation lock.
- Live apps refresh in parallel over `ags request palette-refresh` (5 s
  timeout); restart apps use the serialized quit/confirm/launch lane. A live
  failure never causes a restart.
- AGS restarts hold `$XDG_RUNTIME_DIR/ags-launch.lock`, wait for the old D-Bus
  name/pid to release, verify the intended instance name, and treat exit 0 as
  delegation only when that name is owned (BUG-006).
- Clipboard config, history and images migrate once from `hypr-pano` to
  `clipboard`, retaining favorites/recency and preferences.
- `CONTRACT_PIN` moves to contracts `5eb01b8`, with embedded and
  conformance-checked AGS manifest, install-list, program-tuple and
  refresh-result schemas.

See [`CHANGELOG.md`](CHANGELOG.md) for the full entry.

## Architecture

The package is a hexagon: `domain` (pure, zero-I/O) → `ports` → `adapters` → `application` →
`cli`. The layers and their allowed imports are enforced by `tests/architecture/test_layering.py`.

```text
src/runtime/
  domain/       models, hub/job tables, vendored generated event binding (pure)
  ports/        protocols: event bus, state, history, reloaders, palette/WEG/ITR
  adapters/     subprocess adapters, JSON store, cache, D-Bus, Hyprland/AGS/GTK4 reloaders
  application/  apply_wallpaper, derive, seed_cache, reconcile, doctor, inspect, clipboard
  cli/          Typer app: wallpaper / reconcile / doctor / icons / inspect / monitors / daemon / …
```

`application/apply_wallpaper.py` is the visible-first chain: apply the wallpaper immediately, then
generate the palette (tpr), effects (weg), icons (itr) and the selected skin (manifest + shared
templates), hash the artifacts into `$XDG_STATE_HOME/dotfiles/cache/<layer>/<sha256>/`, atomically
repoint `current/`, and reload the consumers. A cache hit must invoke **zero** CLI subprocesses;
a skin cache hit re-expands no CSS across wallpaper changes.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the state layout, event bus and adapter
contracts.

## Contracts

- **Event contract (`org.dotfiles.Events`)** — canonical in the `contracts` repo. This package
  vendors the generated Python binding and JSON Schema, and embeds byte-identical state schemas
  plus the AGS app manifest/install-list/program-tuple/refresh-result schemas under
  `src/runtime/adapters/schemas/`. The conformance copies live in `contracts/`; the pin is
  `src/runtime/adapters/CONTRACT_PIN` (a commit, by design). `make contracts-check` is hermetic.
- **CLI invocation contract** — the `cli-invocation-contract` distribution from the same
  `contracts` tag (`pyproject.toml`). Adapters build argv/env and the conformance tests call
  `cli_invocation_contract.testkit`, never duplicate the literals.
- The event contract is versioned by interface (`Events1`/`Events2`); the invocation contract is
  versioned by tag. Both are pinned, never range-matched.

## Install (development)

```bash
make sync                 # uv sync --frozen
make test                 # fast suite (integration excluded)
make gates                # test + lint (src/) + contracts-check + hermetic + lock-check + docs-check
make build                # sdist + wheel in dist/
make test-integration     # needs a real tpr/weg/itr or a provisioned spine
```

As a consumer, pin an immutable tag — never a path and never a branch:

```toml
[tool.uv.sources]
dotfiles-runtime = { git = "https://github.com/RandomGenericUsername/dotfiles-runtime", tag = "v0.1.0" }
```

```bash
uv tool install "git+https://github.com/RandomGenericUsername/dotfiles-runtime@v0.1.0"
dotfiles-runtime version
```

A published tag is never moved or deleted; a fix ships as a new tag.

## What `dotfiles-v3` pins

`dotfiles-v3` addresses this repo as a `{name, repo, tag}` entry in
`dotfiles/provisioning/cli-tools.yaml`, next to `tpr`, `weg` and `itr`:

```yaml
- name: dotfiles-runtime
  repo: https://github.com/RandomGenericUsername/dotfiles-runtime
  tag: v0.1.0
```

The `cli_tools` role shallow-clones the tag into `<install>/cli-tools/dotfiles-runtime/<tag>/`,
records the pin, and installs from that checkout. `runtime_seed` then runs
`dotfiles-runtime wallpaper set <install>/wallpapers/default.png`; `verify` asserts the binary,
checkout path and pin. Provisioning owns the pin, clone, install and systemd units; it never
edits this repo.

## Sibling pins

| Distribution | Tag | Source |
| --- | --- | --- |
| `cli-output` | `v0.1.0` | `https://github.com/RandomGenericUsername/cli-output` |
| `cli-invocation-contract` | `v0.2.0` | `https://github.com/RandomGenericUsername/contracts` |

The three CLIs are **not** Python dependencies: the runtime spawns them. Their versions are pinned
by the composition's `cli-tools.yaml`, and the shared invocation record is the `contracts` tag
above.

## Gates

```bash
make help                 # every verb
make gates                # GATES
make contracts-check      # event-contract conformance (hermetic)
make contract-pin-check   # optional: diff vendored copies vs sibling contracts
make hermetic             # empty HOME/XDG from a throwaway cwd
make ci-gates-parity      # workflow job set == GATES
```

CI runs one job per gate on `ubuntu-latest`; `make test-integration` is local only.

## Release

Version in `pyproject.toml` + `CHANGELOG.md` entry → green gates → annotated `vX.Y.Z` tag → CI
attaches the sdist and wheel to the GitHub Release. PyPI is not used.

## Repo harness

- `AGENTS.md` (with `CLAUDE.md` symlinked to it) is the operating contract: evidence rule, runtime
  charter, worktree discipline, planning-vs-implementation commits.
- `tickets/` is the work ledger (`INDEX.md`, `LIFECYCLE.md`, `_template/`, `_fixtures/`).
- `openspec/` holds the specs and the change log (`config.yaml`, `specs/`, `changes/`).
- `scripts/worktree-guard.sh create <id> <slug>` makes `.worktrees/<id>/` on
  `ticket/<id>-<slug>`; never work on `main`.
- `.agents/skills/` + `skills-lock.json` and `_bmad/` are the hash-pinned `mu` harness.

## License

MIT — see [`LICENSE`](LICENSE).
