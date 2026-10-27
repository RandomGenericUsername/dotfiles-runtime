---
id: BUG-006
title: "AGS restart is not idempotent: async quit, shared bundle and exit-0 misclassified"
kind: bug
status: in-progress
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-002/
branch: ticket/REQ-002-ags-app-refresh
repro: "grep -c 'AGS restart failed for capture: process exited with code 0' ~/.local/state/dotfiles/login-reconcile.log; ps -eo pid,ppid,lstart,args | grep '[a]gs run'"
observed: "29 capture exit-0 warnings, 3 bar exit-0 warnings and 30 ReloadError lines; two ags-hypr-pano wrappers exist, one wrapper's gjs owns io.Astal.capture while the other owns io.Astal.hypr-pano. ags quit is asynchronous and the reloader launches immediately; AGS's remote GApplication path exits 0 when the name is still owned, and the liveness poll treats any exit as failure."
expected: "Wait for the old instance's D-Bus name/process to release before launching; serialize every ags run through the shared launch lock; distinguish a clean delegation from a failed restart; verify the intended name/owner; one wrapper per intended instance; no exit-0 warning on a converged login."
---

# AGS restart is not idempotent: async quit, shared bundle and exit-0 misclassified

## Triage

- `kind: bug` — the restart path can leave an instance dead or running another
  app's bundle; the login reconcile then exits non-zero.
- `lane: 2` — bounded adapter fix + regression tests; no OpenSpec change.
- `severity: high` — one shared `/run/user/$UID/ags.js` bundle and asynchronous
  quit make duplicate/incorrect instances reachable at every login.
- Evidence: `ags-apps/tickets/SPIKE-001-ags-refresh-patterns/ticket.md` root
  causes 2/3; current `src/runtime/adapters/ags_reloader.py:206-257`; live
  `login-reconcile.log` counts; `ps` two pano wrappers.

## Repro

```bash
grep -c 'AGS restart failed for capture: process exited with code 0' ~/.local/state/dotfiles/login-reconcile.log
grep -c 'ReloadError: reload failed for: AgsReloader' ~/.local/state/dotfiles/login-reconcile.log
ps -eo pid,ppid,lstart,args | grep '[a]gs run'
ags list
busctl --user list | grep io.Astal
```

## Observed

- 29 capture exit-0, 3 bar exit-0, 30 ReloadError at the spike snapshot.
- Two `ags run -d ~/.config/ags-hypr-pano` wrappers; one child owns
  `io.Astal.capture`, the other owns `io.Astal.hypr-pano`; no capture wrapper.
- `/run/user/1000/ags.js` is the single shared bundle path.

## Expected

- Confirm the old instance is gone (name release/pid exit) before relaunch.
- Serialize launches with the shared per-user AGS launch lock.
- Treat exit 0 as delegation only after verifying the intended instance still
  owns the name; a surviving old instance means the restart did not happen.
- One wrapper per intended instance; login reconcile converges without exit-0
  warnings.

## Fix notes

- Add a bounded quit wait and ownership check; no launch while the old name is
  owned.
- Use the shared launch lock for every `ags run`.
- Add deterministic tests with a fake `ags`/injected app lister; no GUI or kills.
- The bar's safe restart in the new manifest contract depends on this fix.

## Triage (implementation start, 2026-10-26)

- `kind: bug` — the restart path can leave an instance dead or running another
  app's bundle; it is bounded to one adapter plus regression tests, not a
  feature.
- `lane: 2` — no OpenSpec change; AD-28/AD-30/AD-32 supply the restart
  procedure and the shared launch lock.
- `severity: high` — the failure is reachable at every login while AGS 3.1.0
  has one shared per-user bundle (`$XDG_RUNTIME_DIR/ags.js`).
- `Guard:` `bash scripts/worktree-guard.sh check-clean` was run by the parent
  session before this worktree; the worktree started clean on
  `ticket/REQ-002-ags-app-refresh`.
- `Evidence:` AD-28 restart lane and shared lock
  (`architecture-standalone-app-contract.md:136-155`), AD-30 explicit restart
  (`:177-190`), AD-32 confirmed quit before link/start (`:220-248`);
  SPIKE-001 root causes 2/3 (`SPIKE-001.../ticket.md:205-263`); current adapter
  `src/runtime/adapters/ags_reloader.py` (pre-fix `:198-258`).
- `Verify:` `git -C /home/inumaki/Development/dotfiles/dotfiles-runtime
  rev-parse --abbrev-ref HEAD`; `sed -n '136,155p'
  /home/inumaki/Development/dotfiles/ags-apps/_bmad-output/initiative-ags-standalone-apps/architecture-standalone-app-contract/architecture-standalone-app-contract.md`.

## Implementation plan

1. **Bounded quit confirmation before launch.** After `ags quit` (tolerating a
   non-zero exit exactly as before), poll the live AGS names for up to 5 s.
   Also wait for a discovered old wrapper pid to exit when the lister provides
   one. If the intended name is still owned, return failure without spawning
   `ags run` — a second launch would only delegate to the surviving old
   instance.
2. **Shared per-user launch lock.** Every `ags run` in this adapter is issued
   while holding `flock($XDG_RUNTIME_DIR/ags-launch.lock)` (headless fallback:
   a per-uid temp path). The lock is held across quit → release wait → launch
   → name registration, bounded to ~10 s. Other `ags run` callers must adopt
   the same lock (follow-ups below).
3. **Verify the intended name/owner.** Query `ags list` (injectable for tests)
   and require the intended instance name (`ags` for the bar, the app instance
   for tools) to be registered after the launch. Poll for up to 5 s.
4. **Classify clean exit 0 correctly.** A clean exit 0 is GApplication
   delegation, not a crash. It succeeds only when the intended name is owned
   afterwards; exit 0 with no owner is a real failure. A non-zero exit is
   always a failure.
5. **One restart per intended instance.** De-duplicate multiple stale wrappers
   for the same instance before the restart loop; never quit the instance just
   relaunched because a second snapshot entry exists.
6. **Preserve existing behavior.** Missing binary still returns `False` with
   the same warning; bar/tool argv, skip-list, detached `Popen`, and reconcile
   `reload_failures` semantics are unchanged.
7. **Deterministic regression tests.** Fake `ags` (`instance_lister`, `Popen`,
   `subprocess.run`) and an injected app lister; no GUI, no host process kills,
   no host lock writes.

## Red evidence (pre-fix tree)

Evidence (fake `ags` subprocesses, no host process touched):

```text
case A: run exits 0 while io.Astal.capture is still owned
WARNING AGS restart failed for bar: process exited with code 0
reload_ok = False run_calls = 1

case B: quit is asynchronous, old name still owned at launch
reload_ok = True run_calls = 1   (old code launches without any name check)
```

The pre-fix adapter has no launch lock, no name oracle, and treats every exit
(including a clean GApplication delegation) as failure. `Verify:` the snippet
is reproduced in the implementation commit's regression tests
(`tests/unit/test_ags_reloader.py::TestAgsReloaderQuitRelease` and
`::TestAgsReloaderFailure`).
