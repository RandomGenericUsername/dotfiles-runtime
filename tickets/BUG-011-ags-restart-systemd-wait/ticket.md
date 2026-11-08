---
id: BUG-011
title: "Bar restart uses systemctl restart --wait, which blocks until the service stops again"
kind: bug
status: done
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

## Outcome

Implementation `2df1ec7` (fix + regression tests); release tag `v1.0.2`.

### Exact wait change

- Restart argv: `systemctl --user restart <unit>` (no `--wait`). The default
  `systemctl restart` blocks until the restart job completes; `--wait` was
  removed because systemd 262 `man systemctl` says it "synchronously wait[s]
  for started units to terminate again".
- `DEFAULT_RESTART_TIMEOUT = 90.0` in
  `src/runtime/adapters/ags_systemd_units.py`, passed to `run_systemctl` only
  for the restart command; discovery keeps its 10 s `_COMMAND_TIMEOUT`.
- `AgsReloader(systemd_restart_timeout=…)` is the configurable whole-restart
  deadline. The D-Bus owner wait gets whatever remains of that deadline.
- `DEFAULT_INSTANCE_READY_TIMEOUT = 30.0` (3× the unit wrapper's
  `--register-timeout 10`) is the D-Bus readiness bound, also configurable via
  `AgsReloader(instance_ready_timeout=…)`.
- The production readiness check stays the `NameOwnerChanged` event; the
  `ags list` branch is only the headless/test fallback.

### Bound evidence

Live journal stop→start gaps for `dotfiles-ags-bar.service` on 2026-11-07
(`journalctl --user -u dotfiles-ags-bar.service -o short-precise`): 8.939 s,
9.142 s, 7.308 s, with several sub-second fast restarts. The worst observed
gap is 9.142 s, so 90 s is ~10× the observed startup and leaves room for the
unit's `--lock-timeout 10` + `--register-timeout 10` plus a slow bundle load.
The old failure record was `durationMs: 10025` with code 124; it now succeeds
with the same unit.

### Live proof

Command run from the worktree against the real user unit:

```text
uv run python - <<'PY'  # AgsReloader().restart_deployed_app(instance="ags", config_dir=...)
{
  "ok": true,
  "argv": ["systemctl", "--user", "restart", "dotfiles-ags-bar.service"],
  "error": "",
  "elapsed_s": 0.316,
  "old_main_pid": 1184254,
  "new_main_pid": 1185552,
  "old_bus_owner_pid": 1184284,
  "new_bus_owner_pid": 1185591,
  "old_pid_gone": true,
  "new_owner_alive": true
}
PY
```

`systemctl --user show dotfiles-ags-bar.service -p ActiveState -p SubState
-p MainPID -p Result` → `active running 1185552 success`; `busctl --user
status io.Astal.ags` → new owner `PID=1185591`, child of the unit MainPID in
`dotfiles-ags-bar.service`. No `apply` was run.

### Gates

```sh
make gates
# test: 2408 passed, 3 skipped, 11 deselected
# lint: All checks passed
# contracts-check: 58 passed, 1 skipped
# hermetic: 2408 passed, 3 skipped, 11 deselected
# lock-check: Resolved 25 packages
# docs-check: rc 0

make gates-ci
# same six gates; exit 0
```

Integration proof (not a stock gate): `uv run pytest -q
tests/integration/test_ags_systemd_restart_timeout_integration.py` → `1
passed in 15.03s` for a fake `systemctl` that takes 15 s and then marks the
instance ready.
