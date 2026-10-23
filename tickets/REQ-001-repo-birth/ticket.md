---
id: REQ-001
title: "Birth dotfiles-runtime as its own mu-strict repo"
kind: requirement
status: done
lane: 3
links:
  openspec: repo-birth
  bmad: none
worktree: none (new-repo birth; branch in the primary checkout)
branch: ticket/req-001-repo-birth
---

# Birth dotfiles-runtime as its own mu-strict repo

## Requirement

`dotfiles-runtime` SHALL become a standalone public repo, extracted from `dotfiles-v3/src/runtime`
with history preserved, so the runtime has its own release cadence and the composition repo can
consume it as an immutable `{name, repo, tag}` entry.

The extraction changes **no runtime behaviour**: the same package, adapters, CLI surface, state
layout and contract pins move. The only code changes are the path assumptions that named the
composition checkout (`tests/**` root discovery), the repository metadata, and the harness.

This ticket also moves the event-contract conformance material (`contracts/` copies and
`scripts/contract-pin-check.py`) into this repo. The runtime already vendors the generated binding
and embeds the state schemas, so the conformance copy has no other consumer in `dotfiles-v3`.

## Acceptance

- [x] History is preserved: `git log --oneline | wc -l` → 148 imported commits, and
  `git log --diff-filter=A -- src/runtime/pyproject.toml` names the original scaffold commit.
- [x] The mu harness is byte-identical at every copied path: `scripts/worktree-guard.sh`,
  `skills-lock.json`, `_bmad/`, `.agents/skills/`, `.opencode/`, `tickets/_template/`,
  `tickets/_fixtures/`, `tickets/LIFECYCLE.md`. Verify: `diff -r /home/inumaki/Development/mu/<path> <path>` → empty, exit 0.
- [x] `AGENTS.md` carries the evidence rule and the runtime charter; `CLAUDE.md` is a symlink.
  Verify: `rg -c "Evidence:|Verify:" AGENTS.md` → ≥ 2; `test -L CLAUDE.md` → exit 0.
- [x] The repo is composition-free: no `dotfiles/` manifests, no Ansible roles, no `src/cli-tools`,
  no GUI source, no container mode. Verify: `test ! -e dotfiles && test ! -e ansible` → exit 0;
  `rg -n "provisioning|color_scheme_generator|wallpaper_effects_generator|icon_templates_renderer" src/runtime --glob '*.py'` → no import hits (spawn-only adapters).
- [x] Contract ownership moved: `contracts/{event-contract.json,event-contract.xml,schemas/}` and
  `scripts/contract-pin-check.py` exist here; `dotfiles-v3` still has its copies until the
  consumer-rewire slice (destructive-last).
- [x] A fresh clone passes `make sync && make gates && make build`; `make hermetic` is green with
  empty `HOME`/`XDG_*`; `make ci-gates-parity` prints `PASS`.
- [x] `openspec validate --all --strict` exits 0.
- [x] Annotated `v0.1.0` is cut only after the gates and the public repo exist; a published tag is
  never moved.

## Local birth evidence

All commands run from the repo root unless a path is named.

| Claim | Command | Result |
| --- | --- | --- |
| Imported history | `git log --oneline \| wc -l` before the birth commits | `148`; first runtime scaffold `81cdff06` (2026-08-24) |
| Harness byte-identical | `diff -r /home/inumaki/Development/mu/<path> <path>` for the eight copied paths | empty, exit 0 for all eight (`.opencode/` includes `commands/` + `skills/`) |
| Runtime charter + symlink | `rg -c "Evidence:\|Verify:" AGENTS.md`; `test -L CLAUDE.md` | `2`; exit 0 |
| Composition-free | `test ! -e dotfiles && test ! -e ansible && test ! -e src/cli-tools && test ! -e src/shared` | exit 0; no import of provisioning/CLIs in `src/runtime` |
| Gates | `make gates` | `2116 passed, 2 skipped, 18 deselected`; lint `All checks passed!`; contracts `40 passed`; hermetic `2116 passed, 2 skipped, 18 deselected`; lock `Resolved 25 packages`; exit 0 |
| Hermetic cwd | `make hermetic` | exit 0 from a `mktemp -d` cwd with empty `HOME`/`XDG_*` |
| Contract pin | `make contract-pin-check` | `PASS — vendored bindings match …/contracts/generated at pin dbdc5b8` |
| OpenSpec | `openspec validate --all --strict` | `✓ change/repo-birth`, `1 passed, 0 failed` |
| Workflow parity | `make ci-gates-parity` | `PASS gates=[…6…] jobs=[…8…] exempt=['ci-gates-parity', 'release']` |
| Packaging | `make build` | `dotfiles_runtime-0.1.0-py3-none-any.whl` + `.tar.gz`; wheel carries `runtime/adapters/CONTRACT_PIN`, the embedded schemas and `contract_generated.py` |
| Sdist scope | `tar tzf dist/dotfiles_runtime-0.1.0.tar.gz` | harness trees absent; `src/runtime`, `tests`, `README`, `CHANGELOG`, `LICENSE` present |

## Closeout

| Claim | Command | Result |
| --- | --- | --- |
| Repo is public | `gh repo view RandomGenericUsername/dotfiles-runtime --json visibility --jq .visibility` | `PUBLIC` |
| `main` CI green | `gh run view 39998199933 --json status,conclusion,event` | `completed` / `success` / `push` |
| Tag CI green incl. release | `gh run view 39998378028 --json status,conclusion,event` | `completed` / `success` / `push`; all six gates + `release` succeeded |
| Annotated tag | `git rev-parse v0.1.0`; `git rev-parse 'v0.1.0^{commit}'`; `git cat-file -t v0.1.0` | `4d8190a5c8f27ab6a640c9a1dba69aef9562dd4d`; `b38e450f0627b8e0f35c749ed70b956d5abe2850`; `tag` |
| Tag anonymously fetchable | `GIT_TERMINAL_PROMPT=0 git -c credential.helper= ls-remote https://github.com/RandomGenericUsername/dotfiles-runtime 'refs/tags/v0.1.0'` | one line, `4d8190a… refs/tags/v0.1.0` |
| Release carries both artifacts | `gh release view v0.1.0 --json assets --jq '.assets[].name'` | `dotfiles_runtime-0.1.0-py3-none-any.whl`, `dotfiles_runtime-0.1.0.tar.gz` |
| OpenSpec archived | `openspec validate --all --strict` | `spec/repo-harness` valid; change archived as `2026-10-23-repo-birth` |

The `dotfiles-v3` consumer rewire and `src/runtime/` deletion are a separate ticket in that repo
(destructive-last): change `cli-tools.yaml` from `source: src/runtime` to `{repo, tag}`, add the
runtime to `verify`, move/retire the runtime-only gates, delete `src/runtime/` and its vendored
`contracts/` copy, then prove `make deploy-coverage`, `make gates-ci`, and a fresh bootstrap/verify.

## Out of scope

- Rewiring `dotfiles-v3` (changing `cli-tools.yaml` from `source: src/runtime` to `{repo, tag}`,
  adding the runtime to `verify`, and deleting `src/runtime/` + the runtime-only gates). That is
  the destructive-last slice in the composition repo, after this tag is public.
- Renaming the import package from `runtime` to `dotfiles_runtime`. The distribution and console
  script are `dotfiles-runtime`; the import name is kept to avoid a 200+ file mechanical churn.
  A rename can be its own later change.
- Any adapter, command, state or contract behaviour change.
- PyPI publication; the channel is the git tag plus GitHub Release artifacts.
