# repo-harness Specification

## Purpose

This capability governs how `dotfiles-runtime` is operated rather than what it renders: the `mu`
harness copied byte-for-byte, the runtime's one-package boundary, contract ownership, the
fresh-clone gate set, consumption by immutable pin, and the tag-only release ritual. It is the
repo-birth contract (spine AD-8, AD-9, AD-12, OPEN-7; `mu/AGENTS.md` §2–§3), recorded so later
harness, boundary or gate changes have a spec to check against.

## Requirements

### Requirement: the mu harness is present and byte-identical

The repo SHALL contain `scripts/worktree-guard.sh` (executable), `skills-lock.json`, `_bmad/`,
`.agents/skills/`, `.opencode/`, `tickets/_template/`, `tickets/_fixtures/` and
`tickets/LIFECYCLE.md`, each byte-identical to `/home/inumaki/Development/mu`.

Source: spine AD-12; `mu/AGENTS.md` §2–§3.

#### Scenario: copied trees match their source

- **WHEN** `diff -r /home/inumaki/Development/mu/tickets/_template tickets/_template` runs
- **THEN** the output is empty and the exit code is 0

#### Scenario: a dirty tree is refused

- **WHEN** any tracked file is modified and `bash scripts/worktree-guard.sh check-clean` runs
- **THEN** it exits 1 and prints `main is dirty, address it first`

### Requirement: the runtime is one package with no composition or sibling imports

The repo SHALL author exactly one Python package (`dotfiles-runtime`, import package `runtime`)
and SHALL NOT import the provisioning engine, `csg`, `weg`, `itr` or any AGS app. The three CLIs
SHALL be driven only as subprocesses, and the installed spine SHALL be the only production input
(with `DOTFILES_DEV_INPUTS_ROOT` as the explicit, logged development override).

Source: spine AD-1, AD-15, AD-17; `ARCHITECTURE.md` §2/§4/§8.

#### Scenario: no cross-package import

- **WHEN** `src/runtime` is searched for imports of `provisioning`, `core`, `infrastructure`,
  `color_scheme_generator`, `wallpaper_effects_generator` or `icon_templates_renderer`
- **THEN** none exists; the CLI adapters call `subprocess.run`

#### Scenario: the spine is the production source

- **WHEN** derivation inputs are resolved without `DOTFILES_DEV_INPUTS_ROOT`
- **THEN** every provenance value names the install spine, and a missing input fails loud

### Requirement: contract dependencies are pinned by immutable tag

The `cli-invocation-contract` distribution SHALL be consumed by the `contracts` tag in
`[tool.uv.sources]`, and the event contract's pin SHALL be the commit recorded in
`src/runtime/adapters/CONTRACT_PIN`. No range, path or editable install is permitted.

Source: spine AD-4, AD-6, AD-44; `ARCHITECTURE.md` §7.

#### Scenario: the package pin is a tag

- **WHEN** `pyproject.toml` `[tool.uv.sources]` is read
- **THEN** `cli-invocation-contract` carries an `https` git remote and a `vX.Y.Z` tag

#### Scenario: the event pin is a commit

- **WHEN** `src/runtime/adapters/CONTRACT_PIN` is read
- **THEN** it names one commit, and the vendored copies record that same pin

### Requirement: runtime-required contract artifacts are vendored and drift-gated

The generated event binding and state schemas SHALL remain package data, byte-identical to the
canonical `contracts` artifacts. Conformance copies SHALL live under `contracts/`.
`make contracts-check` SHALL prove agreement hermetically; `scripts/contract-pin-check.py` SHALL
fail on a hand-edit.

Source: spine AD-6, AD-44; `ARCHITECTURE.md` §7.

#### Scenario: embedded copies agree

- **WHEN** `make contracts-check` runs
- **THEN** it exits 0 and the embedded schemas and D-Bus XML agree with their canonical copies

#### Scenario: a hand-edit fails

- **WHEN** a vendored generated binding is edited without regenerating it
- **THEN** `make contract-pin-check` (with a sibling `contracts` checkout) exits 1 and names the file

### Requirement: a fresh clone passes the gate set

`make sync && make gates && make build` SHALL exit 0 on a fresh clone, where
`GATES = test lint contracts-check hermetic lock-check docs-check`, and `hermetic` SHALL pass with
`HOME` and all four `XDG_*` variables pointed at empty temp dirs.

Source: spine AD-8, AD-12, AD-14; birth checklist items 9–11.

#### Scenario: gates on a fresh clone

- **WHEN** `git clone <repo> /tmp/x && cd /tmp/x && make sync && make gates` runs
- **THEN** every gate exits 0; `make hermetic` runs the same selection from an unrelated cwd with
  empty `HOME`/`XDG_*`

#### Scenario: build produces both artifacts

- **WHEN** `make build` runs
- **THEN** `dist/dotfiles_runtime-0.1.0-*.whl` and `dist/dotfiles_runtime-0.1.0.tar.gz` exist

#### Scenario: workflow and Makefile agree

- **WHEN** `make ci-gates-parity` runs
- **THEN** it prints `PASS ...` and exits 0; adding a gate without a job (or the reverse) exits 1

### Requirement: the composition addresses this repo by repo and tag

`dotfiles-v3` SHALL address this repo as a `{name: dotfiles-runtime, repo, tag}` entry in
`dotfiles/provisioning/cli-tools.yaml` and the engine role mirror. No `source:` path or editable
install is permitted, and this repo SHALL author no composition manifest or Ansible role.

Source: spine AD-7, AD-10, AD-11; `README.md` "What `dotfiles-v3` pins".

#### Scenario: the manifest entry has one shape

- **WHEN** the `dotfiles-runtime` entry is read from the composition manifest and its engine role
  mirror
- **THEN** both carry `repo` + `tag` (never `source`), and `make deploy-coverage` in the
  composition exits 0 after the consumer-rewire slice

### Requirement: the installed runtime is a tag-bearing checkout

The installed form SHALL be `<install>/cli-tools/dotfiles-runtime/<tag>/` with a `pinned` record
beside it, and the systemd units SHALL run the installed `dotfiles-runtime` binary.

Source: spine AD-10, AD-11; `dotfiles-provision` `roles/cli_tools/tasks/main.yml:111-195`.

#### Scenario: verify sees the pin

- **WHEN** the composition's `verify` runs after the consumer-rewire slice
- **THEN** it asserts the tag-bearing checkout, the `pinned` record and the installed binary

### Requirement: a release is a tag with reproducible artifacts

An annotated `vMAJOR.MINOR.PATCH` tag SHALL be created only after the gates are green,
`CHANGELOG.md` SHALL describe the release, and CI SHALL attach the sdist and wheel built from that
commit to the GitHub Release. A published tag is never moved or deleted.

Source: spine AD-9; OPEN-3 (git-tag-only, no PyPI); OPEN-5 (MIT `LICENSE` before the first tag).

#### Scenario: the first tag resolves to one commit

- **WHEN** `git ls-remote --tags origin v0.1.0` runs with credentials disabled
- **THEN** it prints exactly one line ending in `refs/tags/v0.1.0`, and
  `git rev-parse 'v0.1.0^{commit}'` names the released commit

#### Scenario: the release carries both artifacts

- **WHEN** `gh release view v0.1.0 --json assets --jq '.assets[].name'` runs
- **THEN** it lists `dotfiles_runtime-0.1.0-py3-none-any.whl` and `dotfiles_runtime-0.1.0.tar.gz`
