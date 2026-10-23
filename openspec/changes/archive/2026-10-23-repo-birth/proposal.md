# repo-birth

## Why

`dotfiles-runtime` is the last program still authored inside the composition repo (`dotfiles-v3`).
`ARCHITECTURE.md` and `openspec/specs/composition-closeout/spec.md` describe that repo as
"composition-only" but carve out `src/runtime/` as its one authored tree; `deploy-coverage`
mechanically asserts that exception (`exactly one source: dotfiles-runtime -> src/runtime`). The
runtime already has a clean logical boundary — its own `pyproject.toml`, `uv.lock`, console
script, hexagonal layering, tag pins, and a hard prohibition on importing provisioning code — so
the remaining coupling is the repository/release boundary, not the code.

The CLI/contract split that deferred this extraction (OPEN-7: "runtime stays the host until the
contract and CLI split are stable") is now shipped: `csg`/`weg`/`itr` are at `v0.2.0`, the
invocation contract is `v0.2.0`, and the runtime already consumes both by tag. This change makes
the runtime independently releasable and lets the composition repo become literally composition-only.

Evidence: `dotfiles-v3/ARCHITECTURE.md:34-43` names the two roles; `:401,424` records OPEN-7 as
"out of scope"; `dotfiles-v3/dotfiles/provisioning/cli-tools.yaml:15-16,33-34` carries the one
`source:` entry; `src/runtime/pyproject.toml:25-27` already pins the two external packages by tag.
Verify: `rg -n "dotfiles-runtime|src/runtime" dotfiles-v3/dotfiles/provisioning/cli-tools.yaml`.

Evidence: the runtime does not import provisioning or a CLI.
Verify: `rg -n "provisioning|color_scheme_generator|wallpaper_effects_generator|icon_templates_renderer" src/runtime --glob '*.py'` → no import matches; the adapters call `subprocess.run`.

## What Changes

- Publish `RandomGenericUsername/dotfiles-runtime` as a public repo, default branch `main`, with
  the runtime's 148-commit history imported from `dotfiles-v3/src/runtime` (`git subtree split`).
- Install the `mu` harness **byte-for-byte**: `scripts/worktree-guard.sh`, `skills-lock.json`,
  `_bmad/`, `.agents/skills/`, `.opencode/`, `tickets/_template/`, `tickets/_fixtures/`,
  `tickets/LIFECYCLE.md`.
- Add the repo contract: `AGENTS.md` (evidence rule, runtime charter, ticket routing, worktree
  discipline, planning-vs-implementation, gates/release), `CLAUDE.md` → `AGENTS.md`.
- Add the work ledger (`tickets/INDEX.md`, `tickets/REQ-001-repo-birth/`), the OpenSpec root
  (`openspec/config.yaml`, `openspec/specs/repo-harness/`) and this change.
- Add a fresh-clone `Makefile` (`sync`, `test`, `test-integration`, `lint`, `format`,
  `contracts-check`, `contract-pin-check`, `hermetic`, `lock-check`, `docs-check`, `gates`,
  `gates-ci`, `build`, `install`, `clean`, `ci-gates-parity`), `.github/workflows/gates.yml`,
  `scripts/ci-gates-parity.py`, MIT `LICENSE`, `README.md`, `CHANGELOG.md`, `.gitignore`.
- Move the event-contract conformance material with the runtime: `contracts/event-contract.{json,xml,md}`,
  `contracts/schemas/`, and `scripts/contract-pin-check.py`. The vendored generated binding,
  `contract_generated.schema.json` and the four embedded state schemas stay package data (the wheel
  cannot read a repo path).
- Fix the tests that named the composition layout: `tests/integration/conftest.py` (`parents[4]`
  → repo root), `tests/unit/conftest.py` root discovery (now finds `contracts/schemas/` at this
  repo's root), and the architecture guard paths.
- Update `pyproject.toml` metadata (`readme`, MIT license, classifiers, sdist `only-include`,
  the `integration` marker) and refresh `uv.lock`.

## Capabilities

- **New Capabilities:** `repo-harness` — the harness, one-package boundary, contract ownership,
  gate set, pin consumption and release ritual that make this a valid extraction target.

- **Modified Capabilities:** none. Every runtime command, adapter, artifact contract and state
  schema is unchanged.

## Non-goals (out of scope)

- **Not** rewiring or deleting `dotfiles-v3` content. The composition pin bump
  (`source: src/runtime` → `{repo, tag}`), the `verify` runtime assertions, the deletion of
  `src/runtime/`, the runtime-only gates (`contracts-check`, `runtime-architecture`,
  `contract-pin-check`) and the docs/spec updates are the destructive-last slice in that repo,
  after this repo's `v0.1.0` is public and green.
- **Not** renaming the import package `runtime` → `dotfiles_runtime`. The distribution and console
  script are already `dotfiles-runtime`; the import rename is mechanical churn with no consumer
  benefit today and can be its own change.
- **Not** changing runtime behaviour, adapters, CLI surface, cache format or contract pins.
- **Not** adding a container mode, Dockerfile or `oci-runtime` dependency.
- **Not** publishing to PyPI: the channel is a git tag plus GitHub Release artifacts.

## Impact

- New: `AGENTS.md`, `CLAUDE.md` (symlink), `README.md`, `CHANGELOG.md`, `LICENSE`, `Makefile`,
  `.gitignore`, `.github/workflows/gates.yml`, `scripts/ci-gates-parity.py`,
  `scripts/contract-pin-check.py`, `contracts/`, `docs/ARCHITECTURE.md`, `openspec/`, `tickets/`,
  and the copied `mu` harness.
- Modified: `pyproject.toml` (metadata, sdist, marker), `uv.lock`, and the path assumptions in
  `tests/integration/conftest.py`, `tests/unit/conftest.py` and
  `tests/architecture/test_no_retired_sources.py`.
- Unchanged: every runtime source file, command, adapter, artifact contract and state schema.
- `dotfiles-v3` is untouched by this change.
