# ARCHITECTURE.md — `dotfiles-runtime`

## 1. What this repo is

`dotfiles-runtime` is the host-side program that keeps the desktop converged on the state recorded
in `$XDG_STATE_HOME/dotfiles/current.json`. It is a hexagonal Python package with one console
script (`dotfiles-runtime`) and no composition, engine, GUI or asset content.

It owns:

- the visible-first `wallpaper set` chain (apply → derive → cache → swap → reload → persist),
- the content-addressed cache and `current/` symlink set,
- the JSON state + append-only history store,
- the resident D-Bus daemon (`org.dotfiles.Events`) and clipboard watcher,
- `reconcile`, `doctor`, `inspect`, `icons` and `capture` surfaces.

It does **not** own: the installer engine (`dotfiles-provision`), the composition manifests
(`dotfiles-v3`), the asset corpus (`dotfiles-assets`), the AGS apps/platform, or the CLI sources
(`tpr`/`weg`/`itr`).

## 2. Layering

```text
domain        pure, zero-I/O models, hub/job tables, vendored generated event binding
  ↑
ports         protocols for the event bus, state/history, cache, reloaders, CLI providers
  ↑
adapters      JSON store, cache, D-Bus, Hyprland/AGS/GTK4 reloaders, tpr/weg/itr subprocesses
  ↑
application   apply_wallpaper, derive, seed_cache, reconcile, doctor, inspect, clipboard
  ↑
cli           Typer composition root (`runtime.cli.main:app`)
```

`tests/architecture/test_layering.py` enforces the allowed imports, the domain stdlib allowlist,
and the cross-package blocklist (`provisioning`, `core`, `infrastructure`). The domain layer may
not import `os`, `pathlib`, `io`, `subprocess`, `socket` or `shutil`.

## 3. The `wallpaper set` chain

1. **Apply** the image immediately (visible-first), so the desktop never waits on derivation.
2. **Derive** the wallpaper entry, palette, effects, icons and selected skin:
   - `tpr generate <wallpaper> --format …` (8 formats),
   - `weg batch all -- <wallpaper>`,
   - `itr render [<icons.yaml>]`,
   - skin manifest (`schema_major = 1`, `name`, six token groups) + shared
     `<configDir>.gtk.css.j2` templates (`{{SKIN_*}}` expansion, palette-independent).
3. **Cache** every artifact by its content hash under
   `$XDG_STATE_HOME/dotfiles/cache/<layer>/<sha256>/`, with a co-located `meta.json`.
   The skin layer is palette-independent: its key is `sha256(templates_hash ‖ manifest_hash)`, so
   wallpaper/palette changes never re-expand CSS.
4. **Swap** `current/*` atomically (tmp link + `os.replace`).
5. **Reload** Hyprland, AGS, kitty and the terminal palette.
6. **Persist** `current.json` and append a `history.jsonl` record.

A cache hit is detected before the subprocess spawn and must invoke **zero** `tpr`/`weg`/`itr`
processes. Incomplete legacy entries are evicted and regenerated at the same key
(`application/derive.py`).

## 4. Adapters and the CLI seam

The runtime couples to a CLI by **subprocess, exit status, artifact filenames and hashes**. It does
not import the CLI, parse stdout as JSON, or `command -v` to choose a binary.

| Adapter | argv | Injected env | Artifacts |
| --- | --- | --- | --- |
| `tpr_adapter.py` | `tpr generate <wallpaper> --format …` (8 formats) | `THEME__OUTPUT__DIRECTORY`, `THEME__OUTPUT__OVERWRITE=true`, `THEME__GENERATION__ENGINE`, `THEME__RUNTIME__MODE`, `THEME__TEMPLATES__DIRECTORY` | eight palette files, hashed |
| `weg_adapter.py` | `weg batch all -- <wallpaper>` | `WALLPAPER__OUTPUT__DIRECTORY` | effect images, hashed |
| `itr_adapter.py` | `itr render [<icons.yaml>]` | `ICON_RENDERER__OUTPUT__OUTPUT_DIR`, `ICON_RENDERER__COLOR_SCHEME__PATH`, `ICON_RENDERER__TEMPLATES__DIR` | SVGs, hashed |

The env is an allowlist (`adapters/env.py`): exact keys (`PATH`, `HOME`, `XDG_*`, locale, TLS,
proxy), prefixes `THEME__`, `WALLPAPER__`, `ICON_RENDERER__`, `XDG_`, and a secret blocklist
(`TOKEN`, `SECRET`, `PASSWORD`, `AWS_`, `GITHUB_`).

The record in `contracts/invocation-contract.json` is the source of truth; conformance tests call
`cli_invocation_contract.testkit` rather than duplicating argv/env/artifact literals.

## 5. State, cache and history

```text
$XDG_STATE_HOME/dotfiles/
  current.json                         # desired state (machine-defined, schema-validated)
  current/                             # symlinks: colors.{conf,yaml,gtk.css,adw.css,
                                       #   sequences,rasi,kitty,btop.theme}, icons/
  current/appearance -> cache/appearance/<appearanceHash>/   # ONE v2 hop
  current/skins/<configDir>.css        # v1 pointer, retained during migration
  current/skin.json                    # v1 token map, retained during migration
  cache/appearance/<appearanceHash>/   # colors.yaml + tokens.json + appearance.json
                                       #   + one composed <configDir>.css per target
  cache/<layer>/<sha256>/meta.json     # layers: wallpapers,palettes,effects,icons,skins
  history.jsonl                        # append-only, atomic, symlink-guarded
```

### Appearance plane v2

The palette and skin caches are independent inputs; the runtime composes them
into one immutable generation under `cache/appearance/<appearanceHash>/`:

- `appearanceHash = sha256("appearance.v2\n" + paletteHash + "\n" + skinHash +
  "\n" + mode)`; the directory name is the hash.
- `tokens.json` is the resolved v2 token document (validated against
  `appearance-json.schema.json`). Mode overrides from a v2 manifest replace
  their same-shape group before expansion. During the assets migration window
  a v1 `skin.json` is translated through the RFC §5.2 compatibility mapping.
- `appearance.json` is metadata only (`version: 2`, `skin`, `mode`,
  `paletteHash`, `skinHash`, `appearanceHash`, `generationHash`, `tokenRef`)
  and is written last inside staging.
- Each `<configDir>.css` is the single composed sheet an app installs: palette
  legacy CSS (`colors.gtk.css` bytes), then the rendered palette role block
  (`@define-color <role> <hex>;`), then the expanded skin CSS.
- `current/appearance` is the only repointed hop. Stable
  `config/<configDir>/appearance.css` links point at
  `current/appearance/<configDir>.css`. v1 `skin.css`/`skin.json` pointers
  keep being published until `REQ-015-appearance-v1-pointer-removal`.

**Mode source.** Generation inspects `colors.yaml` for a
`mode`/`scheme`/`appearance`/`variant` scalar (`dark`/`light`) or an
`is_dark`/`dark` boolean. The current `theme-producer` projection
(`src/theme_producer/defaults/templates/colors.yaml.j2`) emits the role map and
legacy `colors:` list but **no mode field**, so the documented fallback is
`dark`. This is a known producer gap: the producer must eventually emit the
selected mode so a light palette reaches the appearance plane without an
authored `mode.light` override. Mode is part of the identity: flipping it
produces a new generation even with unchanged palette/skin hashes.

**Manifest input.** `SkinExpander` accepts both the frozen v1
(`schema_major = 1`, flat groups) and the contracts v0.7.0 v2
(`schemaVersion = 2`, nested `groups`, optional `mode.dark`/`mode.light`)
TOML/JSON manifests. The v1 path and its `skin.json` translation stay until the
assets tuple bump. For v2, appearance generation validates the authored
manifest against the vendored `appearance-manifest` schema, applies the whole
selected override group, then expands the flattened placeholders
(`surface.panel.bg` → `{{SKIN_SURFACE_PANEL_BG}}`) with lengths as `px`,
`letter-spacing` as `em` and `font-weight` unitless.

**Retention.** Publication keeps the active generation, the newest keep-N and
entries inside the grace window; older `cache/appearance/*` directories are
removed. Convenience links are pruned only when they are runtime-owned (their
raw target names `current/appearance`) and are no longer in the generation.

- A cache entry name is a 64-char lowercase hex SHA-256; `meta.json` carries
  `hash_algorithm: "sha256"` and per-artifact hashes (`adapters/cache.py`).
- Repoints are atomic and symlink-guarded (`adapters/seeder.py`).
- Consumer pointers: the pinned palette/theme table
  (`adapters/consumer_path_spec.py`) is `ags`, `gtk-3.0`, `gtk-4.0`, `rofi`,
  `btop`. Skin pointers are **derived per generation**: every
  `<configDir>.css` artifact in the active `SkinEntry` yields
  `config/<configDir>/skin.css` → `current/skins/<configDir>.css` and
  `config/<configDir>/skin.json` → `current/skin.json`. There is no app list;
  an AGS app added later gets pointers as soon as its template is in the skin.
  A pointer whose `<install>/config/<configDir>` is absent is skipped (the
  runtime never creates an app config dir). Reconcile prunes stale
  runtime-owned skin symlinks when a generation drops a target; it never
  prunes the pinned palette/theme table or a user-owned file.
- Skins are selected from `desired.json` (`"skin": "<name>"`, default `glass`). A skin is a
  palette/ink mapping + paint + rhythm only (no structure/variants). The canonical manifest is
  `schema_major = 1`, `name`, plus exactly `shape`/`surface`/`ink`/`type`/`rhythm`/`layout`;
  the 24-token vocabulary separates the panel `border-w` from the thinner card/row/badge
  `hairline-w`. Dimensions must be unitless numbers and templates are `<configDir>.gtk.css.j2` →
  `<configDir>.css`. Numeric tokens render `<number>px` and `skin.json` is the grouped
  `version: 1` map with raw numbers. A missing/invalid manifest keeps the previous skin and
  never touches app CSS.
- Clipboard history is separate: `$XDG_STATE_HOME/clipboard/history.json` with images under
  `$XDG_CACHE_HOME/clipboard/` (config at `$XDG_CONFIG_HOME/clipboard/config.json`). The old
  `hypr-pano` paths are a one-time migration source only (AD-35).

## 6. Daemon and events

`dotfiles-runtime daemon run` is the systemd `ExecStart` surface. It owns the well-known bus name
`org.dotfiles.Events` (interface `org.dotfiles.Events1`) and is the single signal hub; jobs expose
`/org/dotfiles/Job` + `org.dotfiles.Job1.Control(action)`. The AGS bar and the GTK4 subscribers
consume the same contract. The daemon is `Type=dbus`, restart-on-loss, with a watchdog; it never
requests a second hub name.

The clipboard watcher is a separate service (`dotfiles-runtime-clipboard.service`) that owns no bus
name and starts after the daemon.

## 6a. Manifest-driven AGS app refresh

After the palette repoint, `reconcile`/`wallpaper set`/`icons regenerate` run the manifest-driven
AGS refresh (`adapters/ags_app_refresh.py`). Discovery reads every deployed link under
`<install>/config` and validates `app.manifest.json` against `app.tsx` and `ags list` (AD-26).
Running live apps refresh in parallel through
`ags request palette-refresh <appearance-hash> -i <instance>` with a 5 s timeout; restart apps
(the bar) go through the serialized BUG-006 quit/confirm/launch procedure and are included even
when down. The appearance hash now includes the skin entry hash alongside palette/effects/icons
and the icon-manifest inputs; the runtime owns the applied-appearance ledger
(`$XDG_STATE_HOME/dotfiles/appearance-ledger.json`), one operation lock, and per-app JSONL records
under `$XDG_STATE_HOME/dotfiles/logs/ags-app-refresh/<runId>/`. `wallpaper.state` remains the
progress/UI event only; it is not a palette-refresh trigger (AD-27).

## 7. Contracts and conformance

The canonical event contract lives in the `contracts` repo. This package consumes it in two forms:

- **runtime data** — vendored generated binding
  `src/runtime/domain/contract_generated.py` and schema
  `src/runtime/adapters/contract_generated.schema.json`, plus the embedded state schemas and the
  AGS app manifest/install-list/program-tuple/refresh-result schemas under
  `src/runtime/adapters/schemas/`. The wheel cannot read a repo path, so these are package data
  by design.
- **conformance copies** — `contracts/event-contract.{json,xml,md}` and `contracts/schemas/` at the
  commit in `src/runtime/adapters/CONTRACT_PIN`. `make contracts-check` proves the embedded copies
  agree; `scripts/contract-pin-check.py` diffs the vendored generated artifacts against a sibling
  `contracts` checkout and fails on a hand-edit.

The CLI invocation contract is the `cli-invocation-contract` distribution from the same
`contracts` tag. One `contracts` tag is pinned by this repo and by all three CLIs; each CLI's
`--version` contract token equals it.

## 8. Boundaries

- **No provisioning import.** The runtime reads provisioning *output* (the installed spine), never
  its code (`tests/architecture/test_layering.py`, `_FORBIDDEN_CROSS_PACKAGE`).
- **No composition read.** Input resolution is spine-only; `DOTFILES_DEV_INPUTS_ROOT` is the
  explicit, logged development override (`application/derive.py`).
- **No CLI import.** `tpr`, `weg` and `itr` are spawned, never imported; no CLI imports another.
- **No app dependency.** The bar and sibling apps depend on `ags-platform`; the runtime exposes
  events/assets to them over D-Bus and the spine, and imports neither.
- **No container mode.** No Dockerfile and no `oci-runtime` dependency in this repo.

## 9. Gates

`make gates` = `test lint contracts-check hermetic lock-check docs-check`. `make test` excludes the
`integration` marker; `make test-integration` runs it for a developer machine. `make hermetic`
re-runs the fast suite with empty `HOME`/`XDG_*` from a throwaway cwd. `make ci-gates-parity` keeps
the workflow job set equal to `GATES`.
