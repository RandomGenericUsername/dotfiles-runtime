# AGENTS.md — dotfiles-runtime operating contract

Canonical agent instructions for this repo. Claude (`CLAUDE.md` symlinks here), OpenCode, and all BMAD/OpenSpec skills must follow this.

## 1. Evidence rule (hard requirement)

No suggestion "in the air". Every technical claim, recommendation, or design decision must carry testable evidence — at least one of:

- **Doc evidence:** file path + line range, or commit SHA, or URL + quoted section. Example: `ArmDevelopmentEnvironment@last-branch: setup/create_project/create_project.py:120-180`.
- **Prototype evidence:** a script/command in-repo that runs and prints the validating output. State the exact command and observed result.
- **Test evidence:** `command run → result` (exit code + key output). Failing-as-expected counts if it validates the claim.

Format: any non-trivial claim gets an `Evidence:` line and a `Verify:` line (how to reproduce in one command). If evidence doesn't exist yet, say `Evidence: MISSING — run <X> to produce it` and do not present the claim as fact.

## 2. Charter — the runtime, and only the runtime

- This repo owns **one package**: `dotfiles-runtime` (import package `runtime`, console script `dotfiles-runtime`). It is the host-side runtime for the dotfiles desktop: wallpaper apply/reconcile, state, history, cache, the D-Bus daemon, and the clipboard watcher.
- **It consumes, and does not author, the rest of the system.** `cli-output` and `cli-invocation-contract` are immutable git-tag pins (`pyproject.toml` `[tool.uv.sources]`). It never imports the provisioning engine, the three CLIs (`csg`/`weg`/`itr`), or any AGS app.
- **It reads the installed spine only** (`$XDG_DATA_HOME/dotfiles`), never a composition checkout. The sole repo-input override is `DOTFILES_DEV_INPUTS_ROOT`, and it is development-only (`src/runtime/application/derive.py`).
- **It drives the CLIs as subprocesses** — argv, allowlisted env, exit status and artifact hashes; never stdout-as-JSON and never an import. The invocation record lives in the `contracts` repo and is consumed through `cli_invocation_contract.testkit`.
- **It is installed by pin, not by path.** `dotfiles-v3` addresses this repo as `{name: dotfiles-runtime, repo: …, tag: vX.Y.Z}` in `dotfiles/provisioning/cli-tools.yaml`; the installer clones the tag, strips `.git`, installs from the checkout, and the systemd units run the installed console script. This repo ships no composition manifest and no Ansible role.
- **Pure Python, no container mode.** There is no Dockerfile, no `oci-runtime` dependency and no image-build verb here. The two container-mode CLIs own their images; this repo only invokes them.
- The event contract is consumed as **vendored generated bindings** plus byte-identical state schemas embedded in the package; the conformance copies under `contracts/` and `scripts/contract-pin-check.py` are the drift gate. The canonical source is the `contracts` repo, pinned by `src/runtime/adapters/CONTRACT_PIN` (a commit, by design) and by the `cli-invocation-contract` tag.
- Prefer executed output over recalled text. Read files, run commands (`rg`, `git log`, `uv`, `pytest`, `openspec validate`), paste results.
- When findings contradict an earlier claim, state the discrepancy and trust the evidence.

## 3. OpenSpec + BMAD routing (via tickets)

- All work starts as a ticket in `tickets/` (`_template/`, vocab + rollup in `INDEX.md`, flow in `LIFECYCLE.md`). Triage with `/triage` (lanes in `tickets/_fixtures/router-lanes.md`).
- Broad scope (product, requirements, architecture, cross-device design) → BMAD. Installed skills are exactly the seven in `skills-lock.json`: `bmad`, `bmad-architecture`, `bmad-build`, `bmad-product-brief`, `bmad-spec`, `bmod-core-tools`, `bmod-method` — say so when a step needs a missing skill instead of improvising.
- Bounded slices (one proposal + spec + design + tasks) → OpenSpec (`/opsx-propose`, `/opsx-apply`, `/opsx-archive`). Artifact rules in `openspec/config.yaml` restate this evidence rule per artifact.
- Never work on main: `scripts/worktree-guard.sh create <id> <slug>` scaffolds `ticket/<id>-<slug>` + worktree, aborts when dirty. Record worktree/branch in the ticket.
- Keep changes small, verifiable, committable. Never mix planning edits with implementation in one turn.

## 4. Gates and CI

- `make gates` = `GATES = test lint contracts-check hermetic lock-check docs-check`. `make gates-ci` is the same set; `make ci-gates-parity` proves `.github/workflows/gates.yml` has one job per gate (plus declared CI-only jobs).
- `make test` runs the fast suite (`-m "not integration"`). `make test-integration` is the developer proof that needs a real installed CLI or a provisioned spine; it is **not** a stock-runner gate.
- `make lint` covers `src/` only, matching the in-tree Makefile this package was extracted with. The imported test modules carry pre-existing findings; modernizing them is a separate ticket, not part of a birth slice.
- `make hermetic` runs the same fast suite with empty `HOME` and all four `XDG_*` roots from a throwaway directory. A suite that is green only on a provisioned host is not green.
- `make contracts-check` is hermetic: embedded schemas equal the canonical copies under `contracts/`, the D-Bus XML agrees with the event-contract JSON, and the vendored binding equals its generated source. `make contract-pin-check` is the optional positive cross-repo diff; it skips (exit 0) without a sibling `contracts` checkout.
- CI runs only stock-runner jobs. The desktop stack is not a CI dependency; integration proofs are local.
- Release is an annotated `vMAJOR.MINOR.PATCH` tag after green gates, with the sdist and wheel built from that commit attached to the GitHub Release. PyPI is not a channel. A published tag is never moved or deleted; a fix is a new tag.
- Planning edits (tickets/specs/proposals) and implementation edits (code/packaging/harness) land in separate commits.
