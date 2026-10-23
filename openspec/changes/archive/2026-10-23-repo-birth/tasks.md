# Tasks

## 1. Import and scaffold (≤2h)

- [x] 1.1 Import the runtime history from `dotfiles-v3/src/runtime` with
  `git subtree split --prefix=src/runtime`; default branch `main`.
  Verify: `git log --oneline | wc -l` → 148; `git ls-tree --name-only HEAD` →
  `pyproject.toml`, `src/`, `tests/`, `uv.lock`.
- [x] 1.2 Copy the `mu` harness byte-for-byte (`scripts/worktree-guard.sh`, `skills-lock.json`,
  `_bmad/`, `.agents/skills/`, `.opencode/`, `tickets/_template/`, `tickets/_fixtures/`,
  `tickets/LIFECYCLE.md`).
  Verify: `diff` against `/home/inumaki/Development/mu/<path>` for each → empty, exit 0;
  `test -x scripts/worktree-guard.sh` → exit 0.
- [x] 1.3 Write `AGENTS.md` (evidence rule, runtime charter, routing, worktree discipline,
  gates/release) and `CLAUDE.md` → `AGENTS.md`.
  Verify: `test -L CLAUDE.md` → exit 0; `rg -c "Evidence:|Verify:" AGENTS.md` → ≥ 2.
- [x] 1.4 Write `README.md`, `CHANGELOG.md`, `docs/ARCHITECTURE.md`, MIT `LICENSE`, `.gitignore`.
  Verify: `make docs-check` → exit 0.

## 2. Contract ownership (≤1.5h)

- [x] 2.1 Move `contracts/event-contract.{json,xml,md}`, `contracts/schemas/` and
  `scripts/contract-pin-check.py` from `dotfiles-v3`.
  Verify: `test -f contracts/event-contract.json -a -f contracts/schemas/history.schema.json -a -f scripts/contract-pin-check.py` → exit 0.
- [ ] 2.2 Keep the vendored binding, generated schema and embedded state schemas as package data;
  prove they agree with the moved copies and the D-Bus XML.
  Verify: `make contracts-check` → exit 0; `uv build` wheel contains no `contracts/` directory.

## 3. Test path fixes (≤1.5h)

- [ ] 3.1 Replace the composition-layout assumptions: `tests/integration/conftest.py` repo root,
  `tests/unit/conftest.py` walk-up (`contracts/schemas/` now at this repo's root), and the
  architecture guard's `src/runtime` roots.
  Verify: `uv run pytest -q tests/architecture tests/unit/test_contract_schema_conformance.py tests/unit/test_contract_pin_check.py` → exit 0.
- [ ] 3.2 Refresh packaging metadata and `uv.lock`; add the `integration` marker and sdist
  `only-include`.
  Verify: `uv lock --check` → exit 0; `make build` → wheel + sdist.

## 4. Gates and release (≤2h)

- [ ] 4.1 Add the fresh-clone `Makefile` and `scripts/ci-gates-parity.py`; make every verb run
  from `$(CURDIR)`.
  Verify: `make gates` → exit 0; `make ci-gates-parity` → `PASS ...`.
- [ ] 4.2 Add `.github/workflows/gates.yml` with one job per gate, `ci-gates-parity`, and a
  tag-only `release` job.
  Verify: `make ci-gates-parity` → exit 0 (the workflow is parsed, not grepped).
- [ ] 4.3 Prove the fresh-clone path: `make sync --frozen`, `make gates`, `make hermetic`,
  `uv lock --check`, `uv build`.
  Verify: exit 0; `ls dist/dotfiles_runtime-0.1.0-*` lists a wheel and an sdist.
- [ ] 4.4 Add `tickets/INDEX.md`, `tickets/REQ-001-repo-birth/`, `openspec/config.yaml`,
  `openspec/specs/repo-harness/`, and this change.
  Verify: `openspec validate --all --strict` → exit 0.
- [ ] 4.5 Create the public GitHub repo and push `main`; confirm the first CI run is green.
  Verify: `gh repo view RandomGenericUsername/dotfiles-runtime --json visibility --jq .visibility` → `PUBLIC`; `gh run list --limit 1 --json status,conclusion` → `completed/success`.
- [ ] 4.6 Tag annotated `v0.1.0` on the green commit; confirm the Release carries both artifacts.
  Verify: `GIT_TERMINAL_PROMPT=0 git -c credential.helper= ls-remote https://github.com/RandomGenericUsername/dotfiles-runtime 'refs/tags/v0.1.0'` → one line; `gh release view v0.1.0 --json assets --jq '.assets[].name'` → wheel + sdist.

## 5. Closeout (≤0.5h)

- [ ] 5.1 Tick acceptance, record commands + results in the ticket, move the INDEX entry to done.
  Verify: `git status --porcelain` → empty after the merge; `rg -n "REQ-001" tickets/INDEX.md` → the done entry.
