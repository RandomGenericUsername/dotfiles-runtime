---
id: REQ-025
title: "Host-delegated rectangle radii (panel/card/control/cell, contracts v0.11.0)"
kind: requirement
status: in-progress
lane: 2
links:
  contract: contracts/tickets/REQ-019-host-rectangle-radii/ticket.md
  openspec: none
  bmad: none
worktree: .worktrees/REQ-025/
branch: ticket/REQ-025-host-rectangle-radii
---

# Host-delegated rectangle radii (panel/card/control/cell)

## Requirement

Implement the runtime side of `contracts v0.11.0`: a skin may delegate the
outer panel plus the inner `surface.card`, `surface.control` and
`surface.cell` rectangle radii to the live host. `surface.pill.radius` stays
explicit because a pill is a capsule, and `surface.panel.border-width` remains
the only delegable border width. Precedence stays `mode override > host >
skin fallback`, and `HOST_MAP_VERSION` stays `1` so panel-only snapshots keep
their identity.

Scope:

1. **Contract pin.** Move `CONTRACT_PIN` to the contracts `v0.11.0` annotated
   tag object and vendor/embed the updated `appearance-manifest` and
   `generation-manifest` schemas. `make contracts-check` and
   `make contract-pin-check` stay green.
2. **Host map/bounds.** Extend the pure host map with
   `surface.card.radius`, `surface.control.radius` and `surface.cell.radius`
   (all `decoration:rounding`, bounds `0..64`, same numeric rules). Keep
   `surface.pill.radius` absent from the map and the schema allowlist.
3. **Resolver/identity.** Apply the new paths after mode selection and include
   only participating values in `hostHash`; mode overrides, skin fallback,
   headless reads and range rejection behave exactly as for the panel.
4. **Generation manifest.** `host.defaults` and `intent.host` accept the new
   participating paths; equality stays enforced; an old two-path (panel-only)
   snapshot remains valid and unchanged.
5. **Doctor.** Host drift compares only the committed delegated paths, so the
   new radii participate when recorded while pill and non-delegated settings
   are ignored.
6. **Tests.** All five paths, individual card/control/cell, pill rejected,
   precedence/fallback/headless/range, rofi-like card/control/cell tokens
   following the host with pill unchanged, generation defaults/hash/doctor,
   and unchanged GC/current references plus existing suites.
7. **Release.** `v1.4.0` annotated tag after green gates; GitHub Release with
   only the wheel and sdist.

## Acceptance

- [x] `CONTRACT_PIN` is the contracts `v0.11.0` tag object; embedded,
      conformance and pinned schemas agree.
- [x] The host map contains exactly the five allowed paths and rejects
      `surface.pill.radius`; new radius bounds are `0..64`.
- [x] `hostHash` covers only participating values with `HOST_MAP_VERSION = 1`;
      non-delegated host changes cause no identity churn.
- [x] Generation `host.defaults`/`intent.host` accept the new paths, enforce
      equality, and keep old panel-only manifests valid.
- [x] Doctor reports drift only for committed delegated radii and ignores pill
      and non-delegated values.
- [x] Targeted host/skin/generation/doctor suites and `make gates`,
      `make gates-ci`, `make ci-gates-parity` pass.
- [x] `v1.4.0` is an annotated tag on the merged release commit; `main` and the
      tag are pushed; the GitHub Release carries only the wheel and sdist.

## Evidence

- Contract: `/home/inumaki/Development/dotfiles/contracts` `v0.11.0`,
  commit `c49c116` (tag object `6a3ed99`, peeled `9f1c259`); allowlist and
  bounds in `schemas/appearance-manifest.schema.json` and
  `schemas/generation-manifest.schema.json`.
- Runtime map/resolver: `src/runtime/domain/host_defaults.py`.
- Host adapter: `src/runtime/adapters/hyprland_host_defaults.py`.
- Generation identity/validation: `src/runtime/adapters/generation_store.py`.
- Doctor drift: `src/runtime/application/doctor.py::_check_host`.

## Non-goals

- No card/control/cell border-width delegation, no pill delegation.
- No contracts/assets/provisioning/composition/app edit and no apply.
- No `HOST_MAP_VERSION` bump and no generation manifest version bump.

## Verification

Commands run from the worktree root (`.worktrees/REQ-025/`), base `v1.3.1`
(`d98100a`).

| Claim | Command | Result |
| --- | --- | --- |
| Full gate set | `make gates` | PASS — `2563 passed, 3 skipped, 11 deselected`; lint/contracts-check/hermetic/lock-check/docs-check clean |
| CI gate set | `make gates-ci` | PASS — same six gates, `2563 passed, 3 skipped, 11 deselected` |
| Workflow parity | `make ci-gates-parity` | `PASS gates=[...] jobs=[...] exempt=['ci-gates-parity', 'release']` |
| Contracts pin | `make contracts-check` | `58 passed, 1 skipped` |
| Cross-repo pin | `CONTRACTS_REPO=/home/inumaki/Development/dotfiles/contracts make contract-pin-check` | `PASS` at pin `6a3ed99dab6cf3e49a4643bc82e9d7428d0880ca` |
| Targeted host suite | `uv run pytest -q tests/unit/test_host_defaults.py` | `61 passed` |
| Targeted host/skin/generation/doctor | `uv run pytest -q tests/unit/test_host_defaults.py tests/unit/test_skin_expander.py tests/unit/test_skins.py tests/unit/test_appearance.py tests/unit/test_appearance_v2_assets.py tests/unit/test_format_aware_artifacts.py tests/unit/test_generations_v3.py tests/unit/test_generation_gc.py tests/unit/test_doctor_check.py tests/unit/test_contract_schema_conformance.py tests/unit/test_contract_pin_check.py` | `290 passed, 1 skipped` |

### Allowed host paths and bounds

| Host path | Hyprland option | Bounds |
| --- | --- | --- |
| `surface.panel.radius` | `decoration:rounding` | `0..64` |
| `surface.panel.border-width` | `general:border_size` | `0..16` |
| `surface.card.radius` | `decoration:rounding` | `0..64` |
| `surface.control.radius` | `decoration:rounding` | `0..64` |
| `surface.cell.radius` | `decoration:rounding` | `0..64` |

`surface.pill.radius` is not in the map, bounds, schema allowlist or generation
defaults; the resolver rejects it as an unknown inherit path.

### Commits

| Commit | Subject |
| --- | --- |
| `4d914d8` | `docs(REQ-025): plan host-delegated rectangle radii` |
| `77105c6` | `chore(REQ-025): start — record worktree/branch, move to in-progress` |
| `1579d22` | `feat(REQ-025): delegate card/control/cell rectangle radii to the host` |
| release commit (the `v1.4.0` tag target) | `chore(REQ-025): release v1.4.0` |

### Status

**Done on `ticket/REQ-025-host-rectangle-radii`.** The five-path host map,
bounds, resolver identity, generation host defaults and doctor drift are
implemented against contracts `v0.11.0`. Merge to `main` fast-forward, create
the annotated `v1.4.0` tag on the release commit, push `main` + the tag and
publish the GitHub Release with only the wheel and sdist. No apply and no
contracts/assets/provisioning/composition/app edit occurred in this slice.

## Downstream pins needed (not changed here)

- contracts `v0.11.0`
- assets `v0.18.0`
- runtime `v1.4.0`
