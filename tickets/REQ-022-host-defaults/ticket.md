---
id: REQ-022
title: "Host defaults: Hyprland adapter, pure resolver, appearance.v3 and generation host identity"
kind: requirement
status: proposed
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-022/
branch: ticket/REQ-022-host-defaults
---

# Host defaults: Hyprland adapter, pure resolver, appearance.v3 and generation host identity

## Requirement

Implement the Phase 1 host-default runtime for `contracts v0.9.0` and
`dotfiles-assets v0.15.0`. A skin may delegate only
`surface.panel.radius` and `surface.panel.border-width` to the live compositor;
the runtime reads the effective Hyprland values once inside `converge.lock`,
resolves `mode override > host > skin fallback`, and carries the participating
`hostHash` through the appearance and generation identity.

Scope:

1. **Host port/adapter.** `IHostDefaults.read()` and a Hyprland adapter using
   `hyprctl -j getoption` for `decoration:rounding` and
   `general:border_size`. Prefer `HYPRLAND_INSTANCE_SIGNATURE`; otherwise
   discover exactly one instance through `hyprctl -j instances`. Zero/multiple
   instances are unavailable. Validate finite non-negative scalars (radius
   0..64, border 0..16); reject booleans, strings, vectors, NaN/infinity.
   No polling.
2. **Pure resolver.** Resolve after mode selection: a complete selected mode
   group wins, then the host for allowlisted tokens, then the skin value.
   Report provenance (`host`, `skin-fallback`, `mode-override`).
   `fallback = "error"` fails before publishing. `hostHash` covers only the
   mapping version plus participating values.
3. **Skin/appearance.** `resolve_v2_token_document(..., host)` applies the
   policy before token validation/CSS. Do not add host values to
   `skin_entry_hash`; the skin cache remains authored-base. Appearance identity
   moves to `appearance.v3` with the `-` placeholder when nothing participated.
   The composed `tokens.json` and CSS use the resolved values.
4. **Generation identity.** `GenerationStore` writes/validates optional `host`
   (`source`, `version`, `fingerprint`, participating `defaults`) and
   `intent.host`; equality is enforced. Host enters the generation identity.
   Reconcile/seed read the host once inside `converge.lock`. The daemon queue
   key stays host-free; the CLI submits the host-free key and the executing
   job computes the host-aware generation intent under the lock.
5. **GC.** The appearance-cache recomputation must use the host-aware
   `appearance.v3` identity, so host-bearing current/retained cache entries are
   protected.
6. **Doctor.** Compare the committed host fingerprint with a live read over the
   committed participating paths. An unavailable host is unknown/warning, not
   drift. Preserve BUG-016's accepted stable wlogout `appearance.css` link.
7. **Assets fixtures.** Copy `glass`, `paper`, `terminal`, `flat` and all eight
   templates from released `dotfiles-assets v0.15.0` into
   `tests/fixtures/skins-v2/`; prove wlogout still publishes.
8. **Contract pin.** Move `CONTRACT_PIN` and embedded/canonical schema copies
   to `contracts v0.9.0`; `make contracts-check` and
   `make contract-pin-check` stay green.
9. **Release.** `v1.1.0`, annotated tag, fast-forward merge to `main`, push,
   GitHub Release with only the built wheel and sdist.

## Acceptance

- [ ] Host adapter reads both effective options, discovers one instance when
      the signature is absent, and returns unavailable for 0/>1 instances.
- [ ] Invalid values (bool/string/vector/negative/NaN/inf/range) are rejected
      or fall back deterministically; `fallback="error"` fails closed.
- [ ] Resolver precedence/provenance and `hostHash` participation are covered
      by tests; non-delegated host changes cause no identity churn.
- [ ] `appearance.v3` with `-` (no participation) and with a participating
      `hostHash` is implemented; `appearance.json` remains version 2.
- [ ] Generation manifests with host validate; `intent.host` equality is
      enforced; no-host manifests remain valid.
- [ ] Reconcile and seed read the host once under `converge.lock`; the queue
      key stays host-free; CLI/daemon paths pass no host-aware queue key.
- [ ] GC protects a host-bearing appearance cache entry.
- [ ] Doctor reports host drift only for changed delegated values, reports
      unavailable as unknown, and keeps the stable wlogout link accepted.
- [ ] Assets v0.15.0 fixtures are byte-identical; all existing runtime suites
      pass, including appearance v2 assets, generations, GC and doctor.
- [ ] `make gates`, `make gates-ci` and `make ci-gates-parity` pass.
- [ ] `v1.1.0` is an annotated tag on the merged release commit; `main` and the
      tag are pushed; the GitHub Release carries the wheel and sdist.

## Evidence

- Design/criteria: `dotfiles-v3/docs/109-hyprland-host-defaults.md`
  H-2.1–H-2.10; bootstrap report
  `/tmp/opencode/HOST-DEFAULTS-REPORT.md` (ephemeral; reproduce with tests).
- Baseline: `contracts v0.9.0` (tag object
  `c3b1cc7d3fef36042b00d43ae29e2f80087cb184`, peeled
  `5c9fd615cae8d6596799bbafaeea93a05b3c3c11`); `dotfiles-assets v0.15.0`
  (tag `b4fdcf4`); runtime `v1.0.8`.
- Live evidence (read-only): Hyprland 0.56.2 reports rounding `0` and
  border `1` via `hyprctl -j getoption`; config text says 8/3 but
  `hyprland-gui.lua` wins.
- BUG-016: `DoctorUseCase._is_stable_appearance_link` accepts exactly
  `<state>/current/appearance/<config>.css`; the host check must not alter it.

## Verification

Commands run from the worktree root (`.worktrees/REQ-022/`). Filled after
implementation.

| Claim | Command | Result |
| --- | --- | --- |
| Runtime gates | `make gates` | pending |
| Runtime CI gate set | `make gates-ci` | pending |
| Workflow parity | `make ci-gates-parity` | pending |
| Contracts pin | `make contracts-check` | pending |
| Cross-repo pin | `make contract-pin-check` | pending |

### Commits

| Commit | Subject |
| --- | --- |
| pending | `docs(REQ-022): plan the host-defaults runtime` |
| pending | `chore(REQ-022): start — record worktree/branch, move to in-progress` |
| pending | `feat(REQ-022): implement host-default resolution and identity` |
| pending | `chore(REQ-022): release v1.1.0` |

### Status

**Proposed.** No assets/composition/provisioning/apps/contracts change and no
live apply is in this slice.

## Downstream pins (not part of this slice)

- `dotfiles-v3 v0.1.56`: contracts `v0.9.0`, assets `v0.15.0`, runtime
  `v1.1.0`.

## Non-goals

- No additional host mappings (opacity/blur/shadow/gaps) and no socket2
  watcher.
- No other compositor adapter.
- No live apply or provisioning change.
