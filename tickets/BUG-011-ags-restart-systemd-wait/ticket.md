---
id: BUG-011
title: "Bar restart uses systemctl restart --wait, which blocks until the service stops again"
kind: bug
status: in-progress
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-011/
branch: ticket/BUG-011-ags-restart-systemd-wait
repro: "grep -h 'systemctl --user restart dotfiles-ags-bar.service failed with code 124' ~/.local/state/dotfiles/logs/ags-app-refresh/*/00-ags.jsonl | tail -1"
observed: "Every appearance converge records durationMs≈10025, error 'timed out ... after 10.0 seconds', retryable=true for the bar, while systemd later shows ActiveEnterTimestamp matching the same commit and MainPID alive."
expected: "The bar restart returns success once the systemd restart job completes and the new AGS instance owns io.Astal.ags; a genuinely failed start still returns a retryable failure within a bounded, configurable wait."
---

# Bar restart uses `systemctl restart --wait`, which blocks until the service stops again

## Triage

- `kind: bug` — the restart lane reports a healthy bar restart as failed at
  every converge; the bar is left running but the ledger and `doctor` stay
  degraded.
- `lane: 2` — bounded adapter fix plus regression tests; no OpenSpec change.
- `severity: high` — the bar is the visible desktop and every palette apply
  records a false failure with `retryable=true`.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` returned 0 on main
  (`9a5e3a3`) before `create BUG-011 ags-restart-systemd-wait`.
- `Evidence:` systemd 262 `systemctl --help`: "`--wait` For (re)start, wait
  until service stopped again." (`man systemctl`: "When used with start or
  restart, synchronously wait for started units to terminate again ... this
  will wait forever if any given unit never terminates"). The bar is a
  long-running `Type=simple` unit, so `restart --wait` can never return while
  it stays healthy; the 10 s subprocess bound is therefore the only thing
  that ends the call.
- `Evidence:` live ledger/journal
  `~/.local/state/dotfiles/logs/ags-app-refresh/20261108T000643Z-1104686-b9f0ba35/00-ags.jsonl`
  → `durationMs: 10025`, `code 124`, `retryable: true`; journal for the same
  window shows `Stopped` then `Started` with the new gjs and `MainPID`
  1121848 alive; `systemctl --user show ... -p ActiveEnterTimestamp` matches
  the commit.

## Repro

```bash
grep -h 'systemctl --user restart dotfiles-ags-bar.service failed with code 124' \
  ~/.local/state/dotfiles/logs/ags-app-refresh/*/00-ags.jsonl | tail -1
systemctl --help | grep -A1 -- '--wait'
systemctl --user show dotfiles-ags-bar.service \
  -p ActiveEnterTimestamp -p MainPID -p Result
```

## Observed

- Every refresh record for `bar` is `failed`, `retryable: true`,
  `durationMs ≈ 10025`, with
  `error: systemctl --user restart dotfiles-ags-bar.service failed with code 124: timed out ... after 10.0 seconds`.
- The same journal window shows the unit stopped and started, gjs registered
  `io.Astal.ags`, and the unit is `active` with a live `MainPID`.
- The `systemctl --help`/`man` text proves `--wait` means "wait until the
  started unit terminates again", not "wait until it is ready".

## Expected

- `systemctl --user restart <unit>` (job-blocking default) completes the
  stop/start job; then the D-Bus `NameOwnerChanged` seam confirms the new
  instance owns `io.Astal.<instance>`.
- A healthy start that takes 15–30 s succeeds; the bound is at least 60 s and
  configurable.
- A start that never registers fails within the bound and is recorded as a
  retryable failure in the record, ledger and command result.
- No `--wait` in the restart argv and no 10 s regression.

## Fix notes

Root cause is the `--wait` operand, which inverts the wait: it waits for the
unit to stop again. Remove it while keeping the default blocking
`systemctl --user restart` job semantics, and gate readiness on the existing
D-Bus owner event. Use one configurable restart deadline (default 90 s;
observed stop→start gaps up to ~9.1 s plus the unit's `--lock-timeout 10` and
`--register-timeout 10`), with the D-Bus wait capped by the remaining
deadline. Regression tests: 15 s and 25 s simulated starts pass, a never-ready
start fails within a small configured bound, old owner released/new owner
confirmed, failure stays retryable, and `--wait` never appears in argv.
