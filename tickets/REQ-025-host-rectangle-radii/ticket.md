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

- [ ] `CONTRACT_PIN` is the contracts `v0.11.0` tag object; embedded,
      conformance and pinned schemas agree.
- [ ] The host map contains exactly the five allowed paths and rejects
      `surface.pill.radius`; new radius bounds are `0..64`.
- [ ] `hostHash` covers only participating values with `HOST_MAP_VERSION = 1`;
      non-delegated host changes cause no identity churn.
- [ ] Generation `host.defaults`/`intent.host` accept the new paths, enforce
      equality, and keep old panel-only manifests valid.
- [ ] Doctor reports drift only for committed delegated radii and ignores pill
      and non-delegated values.
- [ ] Targeted host/skin/generation/doctor suites and `make gates`,
      `make gates-ci`, `make ci-gates-parity` pass.
- [ ] `v1.4.0` is an annotated tag on the merged release commit; `main` and the
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
