# dotfiles-runtime

The host-side runtime for the dotfiles desktop: wallpaper apply → derive → reconcile, the
content-addressed cache, the state/history store, the D-Bus daemon and the clipboard watcher.

This is the program the composition installs (`dotfiles-v3` → `dotfiles/provisioning/cli-tools.yaml`)
and the systemd user units run. It is **not** the installer, the asset corpus, the GUI, or a CLI
tool: it consumes those by pin and drives `csg` / `weg` / `itr` as subprocesses.

## Public surface

```console
dotfiles-runtime wallpaper set IMAGE      # visible-first apply → derive → reconcile → persist
dotfiles-runtime reconcile                # repoint current/ to current.json
dotfiles-runtime doctor [--repair]        # store vs symlinks vs cache drift
dotfiles-runtime capture                  # resident capture job (hub Control)
dotfiles-runtime clipboard                # resident clipboard watcher
dotfiles-runtime icons regenerate         # re-render the icon set
dotfiles-runtime icons preference …       # light/dark icon preference
dotfiles-runtime gtk4 restart             # reload GTK4 subscribers
dotfiles-runtime daemon run               # systemd ExecStart: org.dotfiles.Events owner
dotfiles-runtime inspect status|daemon|history
dotfiles-runtime inspect cache list|prune
dotfiles-runtime version
```

Global flag: `--format {json,plain,rich}` (via `cli-output`).

## Release notes

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
  ports/        protocols: event bus, state, history, reloaders, CSG/WEG/ITR
  adapters/     subprocess adapters, JSON store, cache, D-Bus, Hyprland/AGS/GTK4 reloaders
  application/  apply_wallpaper, derive, seed_cache, reconcile, doctor, inspect, clipboard
  cli/          Typer app: wallpaper / reconcile / doctor / icons / inspect / daemon / …
```

`application/apply_wallpaper.py` is the visible-first chain: apply the wallpaper immediately, then
generate the palette (csg), effects (weg) and icons (itr), hash the artifacts into
`$XDG_STATE_HOME/dotfiles/cache/<layer>/<sha256>/`, atomically repoint `current/`, and reload the
consumers. A cache hit must invoke **zero** CLI subprocesses.

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
make test-integration     # needs a real csg/weg/itr or a provisioned spine
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
`dotfiles/provisioning/cli-tools.yaml`, next to `csg`, `weg` and `itr`:

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
