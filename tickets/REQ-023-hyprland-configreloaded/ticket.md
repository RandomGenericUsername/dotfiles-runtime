---
id: REQ-023
title: "Hyprland host defaults converge on socket2 `configreloaded` (H-3.4)"
kind: requirement
status: in-progress
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-023/
branch: ticket/REQ-023-hyprland-configreloaded
---

# Hyprland host defaults converge on socket2 `configreloaded` (H-3.4)

## Requirement

Close H-3.4 in `dotfiles-v3/docs/109-hyprland-host-defaults.md`: a Hyprland
`configreloaded` event must re-read the delegated host values and converge
automatically, without a manual `wallpaper set` and without polling. A
`hyprctl keyword` that changes a participating value is the same live
effective-value change; it must be picked up on the event rather than only on
the next unrelated converge.

Scope:

1. **Read-only event source.** Resolve the socket2 path from
   `HYPRLAND_INSTANCE_SIGNATURE` or exactly one `hyprctl -j instances`
   discovery (0/>1 = unavailable, never guessed). Subscribe to
   `$XDG_RUNTIME_DIR/hypr/<sig>/.socket2.sock` and parse `configreloaded`
   protocol lines. A missing/unreadable socket is degraded, never fatal. No
   polling; a short (0.25 s default) debounce coalesces a reload burst.
2. **Daemon integration.** On `configreloaded`, run the one convergence
   function (the same `ConvergenceEngine` / `converge.lock` path) with the
   strict idempotent no-op. Host values are read only inside `converge.lock`
   by the existing Hyprland adapter. An unchanged participating `hostHash`,
   or a change only to non-delegated settings, must leave `current`,
   `current.json`, history and consumers untouched (no restart/refresh). A
   changed participating `hostHash` must commit a new generation and refresh
   consumers.
3. **Health and lifecycle.** Persist a degraded watch-health record when
   socket2 is unavailable, without treating desktop drift as an error.
   Preserve SIGTERM shutdown, queue behavior, watch health, and the one-lock
   invariants.
4. **Tests.** Fake socket2 server coverage: one trigger per debounced burst;
   a delegated rounding/border change updates the generation and doctor;
   non-delegated/unchanged host no-ops without restart; missing socket,
   multiple instances, shutdown during read and malformed lines; no
   polling/sleep-as-correctness.
5. **Release.** `v1.2.0` annotated tag after green gates; GitHub Release with
   only the built wheel and sdist. No provisioning/composition/apps/contracts
   edit and no apply.

## Acceptance

- [ ] A read-only Hyprland socket2 source resolves the path from the instance
      signature or a single discovery, parses `configreloaded`, and degrades
      (no crash, no poll) when the socket is missing.
- [ ] `configreloaded` coalesces to one trigger per debounce window and runs
      the single convergence function without reading host defaults outside
      `converge.lock`.
- [ ] A changed delegated host value commits a new generation and doctor
      reports a match afterwards; an unchanged or non-delegated change
      no-ops without history/consumer restart.
- [ ] Degraded socket health is persisted; SIGTERM, queue, watch health and
      one-lock behavior are preserved.
- [ ] Fake-socket tests cover burst coalescing, delegated/non-delegated
      changes, missing socket, multiple instances, shutdown during read,
      malformed lines and no polling.
- [ ] `make gates`, `make gates-ci`, `make ci-gates-parity` and the targeted
      host/watch/daemon suites pass.
- [ ] `v1.2.0` is an annotated tag on the merged release commit; `main` and
      the tag are pushed; the GitHub Release carries only the wheel and sdist.

## Evidence

- Criteria: `dotfiles-v3/docs/109-hyprland-host-defaults.md` H-3.4 (socket2
  `configreloaded` with short debounce; only a changed participating hostHash
  converges; no fixed sleep/poll).
- Current Phase 1 read site: `src/runtime/application/reconcile.py:379`
  reads `IHostDefaults` inside the `self._mutex.hold(blocking=True)` scope.
- Existing event-coalescing precedent: `src/runtime/application/watch.py`
  (`WatchCoordinator`, 0.25 s debounce, no polling).
- Existing idempotent generation no-op: `GenerationStore.commit` returns
  `unchanged=True` and `ReconcileDesktopStateUseCase` short-circuits the
  current/history/refresh scope when `idempotent=True`
  (`src/runtime/adapters/generation_store.py`, `src/runtime/application/reconcile.py`).
