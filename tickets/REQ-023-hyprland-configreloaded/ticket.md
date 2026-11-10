---
id: REQ-023
title: "Hyprland host defaults converge on socket2 `configreloaded` (H-3.4)"
kind: requirement
status: done
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

- [x] A read-only Hyprland socket2 source resolves the path from the instance
      signature or a single discovery, parses `configreloaded`, and degrades
      (no crash, no poll) when the socket is missing.
- [x] `configreloaded` coalesces to one trigger per debounce window and runs
      the single convergence function without reading host defaults outside
      `converge.lock`.
- [x] A changed delegated host value commits a new generation and doctor
      reports a match afterwards; an unchanged or non-delegated change
      no-ops without history/consumer restart.
- [x] Degraded socket health is persisted; SIGTERM, queue, watch health and
      one-lock behavior are preserved.
- [x] Fake-socket tests cover burst coalescing, delegated/non-delegated
      changes, missing socket, multiple instances, shutdown during read,
      malformed lines and no polling.
- [x] `make gates`, `make gates-ci`, `make ci-gates-parity` and the targeted
      host/watch/daemon suites pass.
- [x] `v1.2.0` is an annotated tag on the merged release commit; `main` and
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

## Verification

Commands run from `.worktrees/REQ-023/`.

| Claim | Command | Result |
| --- | --- | --- |
| Full gates | `make gates` | PASS — `2516 passed, 3 skipped, 11 deselected`; lint/contracts-check/hermetic/lock-check/docs-check clean |
| CI gate set | `make gates-ci` | PASS — same six gates, rc 0 |
| Workflow parity | `make ci-gates-parity` | `PASS gates=[...] jobs=[...] exempt=['ci-gates-parity', 'release']` |
| Targeted host/watch/daemon | `uv run pytest -q tests/unit/test_host_configreloaded.py tests/unit/test_host_defaults.py tests/unit/test_daemon_run.py tests/unit/test_watch_coordinator.py tests/unit/test_watch_health.py tests/unit/test_one_lock.py tests/unit/test_doctor_check.py` | `174 passed` |
| Live socket read-only | `HyprlandConfigSocketSource().start(); status()` | `registered=1 degraded=False failed=() error=None` for signature `efb50993…` |

### Exact event/debounce behavior

- The adapter resolves `HYPRLAND_INSTANCE_SIGNATURE`, else exactly one
  `hyprctl -j instances` entry; 0/>1 instances and a missing
  `$XDG_RUNTIME_DIR` are unavailable. It connects to
  `$XDG_RUNTIME_DIR/hypr/<sig>/.socket2.sock` once at daemon start.
- `read_event` blocks in `select` on the socket. Complete protocol lines are
  drained as one batch; only the event name before `>>` is inspected, so
  `configreloaded>>` (and bare `configreloaded`) match, while workspace,
  unknown and malformed lines are ignored. A partial tail stays buffered
  until its newline.
- `HostEventCoordinator` marks a burst pending on the first event and fires
  exactly one `WatchTrigger(reason="configreloaded")` when the socket is
  quiet for the 0.25 s window. A burst still open at shutdown is not flushed.
- The daemon's callback runs `_run_host_converge` (active mode) through the
  single `ConvergenceEngine` with `idempotent=True`; observe-only remains the
  shipped default. Host defaults are read by `HyprlandHostDefaults` only in
  the reconcile critical section, inside `converge.lock`.
- A changed participating `hostHash` commits, writes one `reactive` history
  line and refreshes consumers; an unchanged or non-delegated host change
  hits `GenerationStore.commit(...unchanged=True)` and returns before
  `current.json`, history, GC, appearance ledger, apply, AGS refresh or
  reloaders.
- Watch health merges inotify and host-source statuses into the one
  `watch-health.json`; an unavailable socket contributes
  `failed=("hyprland:socket2",)` and a reason, without changing doctor's exit
  code.

### Commits

| Commit | Subject |
| --- | --- |
| `238c84d` | `docs(REQ-023): plan socket2 configreloaded convergence` |
| `cfdd2f1` | `feat(REQ-023): converge host defaults on Hyprland configreloaded` |
| release commit (the `v1.2.0` tag target) | `chore(REQ-023): release v1.2.0` |

### Status

**Done on `ticket/REQ-023-hyprland-configreloaded`.** The read-only socket2
source, debounce coordinator, daemon wiring, idempotent host convergence,
merged watch health and fake-socket regression suite are implemented. Merge
to `main` fast-forward, create the annotated `v1.2.0` tag on the release
commit, push `main` + the tag, and publish the GitHub Release with only the
wheel and sdist. No provisioning/composition/apps/contracts edit and no
apply was performed.

## Non-goals

- No additional host mappings, no other compositor adapter, no contracts or
  dotfiles-v3/downstream edit.
- No change to the daemon's observe-only default (`--activate` still gates
  automatic mutations).
