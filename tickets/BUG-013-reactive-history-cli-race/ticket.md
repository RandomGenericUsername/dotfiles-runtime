---
id: BUG-013
title: "Reactive mode changes leave a stale history tail, converge slowly, and reconcile --regenerate-stale returns a pre-commit snapshot"
kind: bug
status: in-progress
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-013/
branch: ticket/BUG-013-reactive-history-cli-race
repro: "Change ~/.config/dotfiles/desired.json from smart to light (no command), wait for the watcher; then run `uv run dotfiles-runtime doctor` and `uv run dotfiles-runtime reconcile --regenerate-stale --format json`."
observed: "doctor ends `diverged | history:tail | history | current.json disagrees with the history tail on palette (tail trigger='reactive' ts='2026-11-08T02:49:26.758111Z')`; the 02:49:26 reactive line carries light palette 35f53fb9bc894bd0186ae4cd8f7969da1ae9aa74af2976f5fee37ee369958eb2 while current.json at 02:49:27 carries dark palette 0797efd74520459ec24c15a9f7abbbcb62f34a506f7fe371bda3bfb7cabc8363 and current/manifest.json generationHash 43d62e17e2da58da2ea43da41bde41fb30e9d0077611a8c34fe30e8d50ef7fc8 mode dark. The same sequence converged in ~42s (smart->light) and ~56.5s (light->smart->dark). `reconcile --regenerate-stale` returned `regenerated: []` and old palette 0797... while the daemon committed 162a08c9d440 (light) and refreshed apps ~2s later."
expected: "Every history line is appended atomically after the generation commit from the committed state, so the history tail, current.json, and current/manifest.json always agree. A watcher mode change fires on the inotify event after a short sub-second debounce, never a 30-60s wait. A successful `reconcile --regenerate-stale` awaits the same daemon converge job (or runs the same single convergence under converge.lock in fallback) and returns the committed generation/state. doctor is clean after a watcher light->smart->dark burst."
---

# Reactive mode changes leave a stale history tail, converge slowly, and reconcile --regenerate-stale returns a pre-commit snapshot

## Triage

- `kind: bug` — three symptoms of one mutation-path split: the reactive
  composite appends its own history line after a separate reconcile commit,
  the watcher-driven path can run concurrently with a CLI mutation, and
  `reconcile --regenerate-stale` still executes a local pre-commit snapshot
  instead of awaiting the single convergence job.
- `lane: 2` — bounded application/composition fix plus regression tests; no
  OpenSpec change.
- `severity: high` — the audit trail is the doctor's store/history signal, and
  a mutating CLI returning a pre-commit palette makes the desktop state
  untrustworthy.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` returned 0 on main
  (`2df57df`) before `create BUG-013 reactive-history-cli-race`.

## Evidence

- `Evidence:` live `~/.local/state/dotfiles/history.jsonl` tail on
  2026-11-07/08:
  `02:49:26.758111Z reactive palette=35f53fb9...` (light) followed by
  `current.json` `applied_at=2026-11-08T02:49:27.338379Z` with
  `palette=0797efd7...` (dark), while `current/manifest.json` records
  `generationHash=43d62e17...` and `mode=dark`.
- `Evidence:` live `~/.config/dotfiles/desired.json` mtime
  `2026-11-07 21:48:29 -05` (02:48:29Z); the next reactive converge logged at
  `02:49:26Z` (~57s later). Journal shows reactive convergence only on the
  minute boundary, not on the `desired.json` write.
- `Evidence:` `src/runtime/application/reconcile.py:473-482` appends history
  outside `converge.lock` after `save()`; `src/runtime/cli/main.py:2258-2270`
  (`_append_reactive`) reloads `current.json` after the composite, so a
  concurrent commit can land between that read and the append.
- `Evidence:` `src/runtime/cli/main.py:3407-3409` sends
  `reconcile --regenerate-stale` straight to the local
  `_run_regenerate_stale`; its reconcile path uses `.seed.lock` for the
  regenerate save and only later enters `converge.lock`, so a daemon commit
  can advance `current.json` after the CLI snapshot is taken.
- `Evidence:` existing coverage already pins the live identity values:
  `tests/unit/test_doctor_check.py:34-46` (dark palette `0797...`,
  generation `43d62e17...`) and `tests/unit/test_doctor_check.py:876-891`
  (reactive tail clean).

## Repro

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
# With the daemon active, flip the intent mode with no command and wait.
jq '.mode="light"' ~/.config/dotfiles/desired.json > /tmp/desired.json && \
  mv /tmp/desired.json ~/.config/dotfiles/desired.json
uv run dotfiles-runtime doctor
uv run dotfiles-runtime reconcile --regenerate-stale --format json
```

## Observed

```text
diverged | history:tail | history | current.json disagrees with the history tail on palette
(tail trigger='reactive' ts='2026-11-08T02:49:26.758111Z')
```

`reconcile --regenerate-stale` printed `regenerated: []` with palette
`0797efd7...` while the daemon later committed `162a08c9d440` (light).

## Expected

- History is appended once, after the generation commit and the compatibility
  `current.json` save, using that committed snapshot, all while
  `converge.lock` is held. A reactive line can never describe a palette older
  than the committed `current.json`/manifest.
- The watcher fires on the inotify event after a short debounce; mode changes
  converge promptly and bursts still coalesce into one trigger.
- `reconcile --regenerate-stale` awaits the same daemon job (or the same
  single convergence under `converge.lock`) and reports the committed palette,
  so the result can never be a pre-commit snapshot.
- `doctor` is clean after a watcher-driven light->smart->dark sequence.

## Fix notes

1. Move `CacheSeeder.append_history` for the v3 reconcile mutation inside the
   `converge.lock` scope, immediately after `state_repo.save(saved)`, and use
   the committed `saved` state. Remove the reactive composite's separate
   post-composite `_append_reactive`; its inner reconcile now owns the single
   `trigger="reactive"` line (still history-suppressed for every other inner
   step).
2. Make every CLI mutation submit the same queue job when the daemon owns the
   bus and fall back to the same convergence under `converge.lock` otherwise.
   Add a `regenerate` flag to the converge request: the queue worker runs the
   existing RegenerateStale pre-step (history-suppressed, no internal
   reconcile) and then the single Reconcile commit; the CLI returns the
   committed generation/state plus the worker's `regenerated` list.
3. Have `ConvergenceEngine` hold `converge.lock` across the optional
   regenerate step and the reconcile run (reconcile runs with the lock already
   held), so no daemon/CLI or CLI/CLI interleave can produce a stale line or
   snapshot.
4. Add timing/regression coverage: real inotify + atomic desired replacement
   triggers within a sub-second bound; history tail == current == manifest
   after a burst using the live light/dark hashes; no stale CLI result;
   concurrent reactive + CLI; doctor clean.

Regression tests:

- `tests/unit/test_watch_coordinator.py` — real inotify source over a tmp
  intent file, atomic replace -> one trigger within a sub-second deadline.
- `tests/unit/test_reactive_converge.py` — burst/history ownership: inner
  reconcile owns the only line; no post-composite snapshot read.
- `tests/unit/test_generations_v3.py` — concurrent reactive+CLI queue and
  engine lock: one committed generation and one history line per result.
- `tests/unit/test_regenerate_stale.py` — `--regenerate-stale` uses the
  committed queue/fallback result, never a pre-commit palette.
- `tests/unit/test_doctor_check.py` / integration reactive E2E — doctor clean
  after mode burst, including the live 35f53fb9/0797efd7 identities.
