---
id: BUG-006
title: "AGS restart is not idempotent: async quit, shared bundle and exit-0 misclassified"
kind: bug
status: triage
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: none
branch: none
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
