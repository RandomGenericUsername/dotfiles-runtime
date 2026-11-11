---
id: REQ-026
title: "Required surface.window role (contracts v0.12.0)"
kind: requirement
status: in-progress
lane: 2
links:
  contract: contracts/tickets/REQ-020-window-role/ticket.md
  openspec: none
  bmad: none
worktree: .worktrees/REQ-026/
branch: ticket/REQ-026-window-surface-role
---

# Required surface.window role

## Requirement

Implement the runtime side of `contracts v0.12.0`: `surface.window` is a
required sixth surface role for the outer/application toplevel. It is never an
alias for `surface.panel` and has no panel fallback. The role owns eight tokens
(`bg`, `border`, `border-width`, `radius`, `padding`, `shadow`, `blur`,
`opacity`); `opacity` is a unitless number in `[0, 1]` and is never derived
from an `alpha()` paint.

Scope:

1. **Contract pin.** Move `CONTRACT_PIN` to the contracts `v0.12.0` annotated
   tag object and vendor/embed the updated `appearance-manifest`,
   `appearance-json` and `generation-manifest` schemas. `make
   contracts-check` and `make contract-pin-check` stay green.
2. **Host map/bounds.** Add `surface.window.radius` (`decoration:rounding`,
   `0..64`) and `surface.window.border-width` (`general:border_size`,
   `0..16`) to `HOST_MAP`/`HOST_BOUNDS`. `surface.window.padding` and
   `surface.window.opacity` are not delegable and not recorded.
3. **Resolver/expansion/identity.** Apply the two window paths after mode
   selection, include only participating values in `hostHash`, keep
   `HOST_MAP_VERSION = 1`, and render `surface.window.opacity` as a unitless
   number in every format (no `px`/`em`, no alpha derivation). No runtime
   panel→window alias or fallback.
4. **Generation defaults.** `host.defaults`/`intent.host` accept the window
   geometry paths when they participate; padding/opacity are rejected by the
   closed schema.
5. **Doctor.** Host drift compares committed delegated window radius/border
   width and ignores padding, opacity and other non-delegated settings.
6. **Tests.** Window group/required tokens and opacity bounds/type; host
   window paths and bounds; fallback/headless/mode-override; generation
   defaults/hash; doctor drift; unitless opacity rendering; existing suites.
7. **Release.** `v1.5.0` annotated tag after green gates; GitHub Release with
   only the wheel and sdist.

## Acceptance

- [ ] `CONTRACT_PIN` is the contracts `v0.12.0` tag object; embedded and
      conformance schemas agree.
- [ ] The host map adds exactly the two window geometry paths and rejects
      window padding/opacity.
- [ ] Window radius/border width participate in `hostHash`; only participating
      values count and `HOST_MAP_VERSION` stays `1`.
- [ ] `surface.window.opacity` renders unitless (`0.94`, never `0.94px` or
      `0.94em`) and is never derived from an alpha paint.
- [ ] Generation `host.defaults`/`intent.host` accept the window paths and the
      closed defaults schema rejects padding/opacity.
- [ ] Doctor reports drift only for committed window geometry paths and
      ignores padding/opacity and other non-delegated values.
- [ ] Targeted host/skin/generation/doctor suites and `make gates`,
      `make gates-ci`, `make ci-gates-parity` pass.
- [ ] `v1.5.0` is an annotated tag on the merged release commit; `main` and the
      tag are pushed; the GitHub Release carries only the wheel and sdist.

## Evidence

- Contract: `/home/inumaki/Development/dotfiles/contracts` `v0.12.0`
  (tag object `b543573904f5515d8eefb4daf361a388b5228c50`, peeled
  `a03b5632828df8012c02a6117c3be438eca4264b`); role and host allowlist in
  `schemas/appearance-manifest.schema.json`, `schemas/appearance-json.schema.json`
  and `schemas/generation-manifest.schema.json`; rules in `appearance.md`
  (`### surface`, host allowlist, stored-vs-rendered values) and
  `generations.md` (`defaults`).
- Runtime map/resolver: `src/runtime/domain/host_defaults.py`.
- Host adapter: `src/runtime/adapters/hyprland_host_defaults.py`.
- Token expansion: `src/runtime/adapters/skin_expander.py` (`_css_v2_value`,
  `_format_v2_value`).
- Generation identity/validation: `src/runtime/adapters/generation_store.py`.
- Doctor drift: `src/runtime/application/doctor.py::_check_host`.

## Non-goals

- No contracts/assets/provisioning/composition/app edit and no apply.
- No runtime panel→window derivation; the migration copies values once in the
  authored manifest.
- No `HOST_MAP_VERSION` or generation manifest version bump.

## Verification

Commands run from the worktree root (`.worktrees/REQ-026/`), base `v1.4.0`
(`e09b6e1`).

| Claim | Command | Result |
| --- | --- | --- |
| Pending | — | — |

## Downstream pins needed (not changed here)

- contracts `v0.12.0`
- assets `v0.19.0`
- platform `v1.1.0`
- engine/composition: re-pin runtime `v1.5.0` (no edit in this slice)
