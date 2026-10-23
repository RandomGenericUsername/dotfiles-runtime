# Design — repo birth

## Decisions

### D1. History is imported; the harness is the second commit

The runtime has real history under `dotfiles-v3/src/runtime` (148 commits, first added in
`81cdff06`). `git subtree split --prefix=src/runtime` preserves it, so the new repo's `main` starts
at the imported history and the birth commit lands on top. A fresh-root import would lose the
"why" of 148 changes.

Evidence: `git log --oneline | wc -l` → 148 after the import.
Verify: `git log --oneline | tail -1`; `git log --diff-filter=A -- src/runtime/pyproject.toml`.

### D2. The import package stays `runtime`; the repo/distribution is `dotfiles-runtime`

The distribution name, console script and systemd `ExecStart` already are `dotfiles-runtime`. The
import package is `runtime`, referenced by every source and test module. Renaming it to
`dotfiles_runtime` is a 200+ file mechanical change with no consumer-facing benefit (the package is
not a library dependency of any sibling; the composition spawns the console script). Keep `runtime`
for the extraction; a rename, if wanted, is a separate change with its own gates.

Evidence: `pyproject.toml` `[project.scripts] dotfiles-runtime = "runtime.cli.main:app"`.
Verify: `rg -n "from runtime|import runtime" src tests | wc -l`.

### D3. Contract materials follow the failure mode, not a single house style

The workspace has three established patterns, and the runtime legitimately uses two of them:

- **package dependency by tag** — `cli-invocation-contract` is consumed from `contracts@v0.2.0`
  (`pyproject.toml`), exactly like the three CLIs;
- **canonical checkout + drift test** — AGS apps check out `contracts` in CI and diff their client
  against it (`CONTRACTS_ROOT`), because they consume the event contract only at test time;
- **vendored artifact + pin-check** — the runtime must carry the event contract at **runtime**: the
  installed wheel cannot read the repo `contracts/` directory, so the generated Python binding and
  schemas are package data. The conformance copies and `scripts/contract-pin-check.py` make the
  second source of truth mechanical.

Moving `contracts/` and the pin-check here is therefore the runtime's established pattern, not a
new one. The composition's copy has no other consumer.

Evidence: `src/runtime/adapters/emit_validation.py` and `gtk4_app_subscriber.py` state the wheel
cannot read the repo path; `dotfiles-v3/scripts/contract-pin-check.py` defines the vendored table.
Verify: `make contracts-check`; `make contract-pin-check` (with a sibling checkout).

### D4. Composition conformance moves out; the composition proves the pin by execution

Today `dotfiles-v3` runs `contracts-check` and `runtime-architecture` via
`uv run --directory src/runtime pytest`. Once `src/runtime/` is deleted, those targets have nothing
to run. This repo owns its full suite (`make test`, `make contracts-check`, architecture guards).
The composition keeps only what is its job: the pin (`deploy-coverage`), the pinned checkout and
`pinned` record (`verify`), and the actual `wallpaper set` seed (`runtime_seed`). That matches the
family pattern: each extracted repo runs its own gates; the composition gates provisioning shape,
not the pinned repo's tests.

Evidence: `dotfiles-v3/Makefile:21-34` is the current runtime-only gate wiring.
Verify (after the consumer slice): `rg -n "src/runtime" dotfiles-v3/Makefile` → no matches.

### D5. `GATES` is the single gate list; CI is compared to it

The workflow has one job per gate so a failure names the gate, and `scripts/ci-gates-parity.py`
parses `GATES` from the `Makefile` and the job keys from the workflow. CI-only jobs
(`ci-gates-parity`, `release`) are declared with a reason; a stale declaration fails.

`test` excludes the `integration` marker because those tests need a real installed CLI or a
provisioned spine; `make test-integration` keeps them one command away.

Evidence: `make ci-gates-parity` → `PASS gates=[...] jobs=[...] exempt=[...]`.
Verify: `make ci-gates-parity`.

### D6. Hermetic means hermetic

`make hermetic` runs the fast suite with `HOME` and all four `XDG_*` roots pointed at fresh temp
dirs. The runtime's production resolution is spine-only, so a host-provisioned spine must not
change a result; tests that need real binaries/inputs already skip with their stated reason (never
vacuously pass).

Evidence: `src/runtime/application/derive.py` spine-only resolution; `tests/unit/conftest.py`
repo-root discovery via `contracts/schemas/`.
Verify: `make hermetic` → exit 0.

### D7. Release is a tag; the composition consumes a checkout

The tag is the immutable release record and CI attaches the sdist and wheel built from the commit.
The composition installs from a **tag-bearing checkout** (not a `git+` URL) so PEP 610 records the
pin and `verify` can assert the checkout path + `pinned` file. The wheel is not the install
mechanism for the composition; it is the release artifact.

Evidence: `dotfiles-provision/ansible/roles/cli_tools/tasks/main.yml:111-195` defines the pinned
checkout install shape.
Verify (after the consumer slice): `<install>/cli-tools/dotfiles-runtime/<tag>/` exists with
`../pinned` equal to the tag.

## Verification plan

| Decision | Command | Expected |
| --- | --- | --- |
| D1 history | `git log --oneline | wc -l` | `148` imported commits before the birth commit |
| D2 name | `make sync && uv run dotfiles-runtime version` | prints `0.1.0` |
| D3 contracts | `make contracts-check` | exit 0, no sibling required |
| D3 positive pin | `CONTRACTS_REPO=… make contract-pin-check` | exit 0, or explicit SKIP when absent |
| D4 boundary | `rg -n "provisioning|color_scheme_generator|wallpaper_effects_generator|icon_templates_renderer" src/runtime --glob '*.py'` | no import matches |
| D5 gates | `make gates` | exit 0 |
| D5 parity | `make ci-gates-parity` | `PASS …` |
| D6 hermetic | `make hermetic` | exit 0 with empty HOME/XDG |
| D7 build | `make build` | wheel + sdist in `dist/` |
