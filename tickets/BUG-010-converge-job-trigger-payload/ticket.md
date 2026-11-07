---
id: BUG-010
title: "Converge jobs write an invalid history trigger and wallpaper.state can be rejected"
kind: bug
status: in-progress
lane: direct
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-010/
branch: ticket/BUG-010-converge-job-trigger-payload
repro: "dotfiles-runtime wallpaper set ~/.local/share/dotfiles/wallpapers/default.png (with the v1.0.0 daemon running)"
observed: "Emit failed: org.dotfiles.Events1.PayloadTooLarge, then wallpaper set failed: converge job b743... failed: ValueError: invalid history trigger: 'daemon' (expected one of doctor, force, prune, reactive, reconcile, regenerate, seed, set)"
expected: "wallpaper set through the daemon writes exactly one valid history line (trigger 'set'); daemon reactions write 'reactive'; wallpaper.state fits the contract; a publish failure is logged without masking the command result"
---

# Converge jobs write an invalid history trigger and `wallpaper.state` can be rejected

## Repro

```sh
dotfiles-runtime wallpaper set ~/.local/share/dotfiles/wallpapers/default.png
```

The v1.0.0 daemon owns `org.dotfiles.Events`, so the command submits a
`converge:<intentHash>` job and waits for `JobFinished`.

## Observed

```text
Emit failed: org.dotfiles.Events1.PayloadTooLarge
ERROR ... wallpaper set failed: converge job b743... failed: ValueError: invalid history trigger: 'daemon' (expected one of doctor, force, prune, reactive, reconcile, regenerate, seed, set)
```

Two defects are visible:

1. `_daemon_converge_worker` hardcodes `ConvergeIntent(trigger="daemon")`.
   `daemon` is a generation-manifest **source kind**, not a history trigger, so
   `ReconcileDesktopStateUseCase.run` rejects it before any mutation.
2. `_publish_wallpaper_state` emits an out-of-contract stage (`converging`,
   `palette`) and also forwards unbounded `error` / `reload_failures` strings.
   The hub classifies both schema and size rejections as `PayloadTooLarge`.

## Expected

- No invalid trigger can reach `history.jsonl`. A CLI `wallpaper set` job
  records `set`; a CLI `reconcile` job records `reconcile`; a daemon reaction
  records `reactive` (its inner steps stay history-suppressed).
- `wallpaper.state` is always a compact contract-valid payload below the
  hub's 64 KiB cap. A publish failure is a visible warning, never a crash or a
  traceback that masks the apply/reconcile result.
- The daemon job path and the no-daemon CLI fallback both converge and write a
  valid history line.

## Fix notes

- Keep `HISTORY_TRIGGERS` unchanged (no `daemon` member): normalize the job's
  requested trigger against the canonical enum and fall back to `reactive` for
  an absent/invalid daemon-queue request. Carry `suppressHistory` /
  `includeTerminal` in the converge request so a reactive composite keeps its
  single `reactive` line through the daemon job path.
- Bound every emitted `wallpaper.state` field (summary error + capped reload
  failures), use only contract stage names, and validate size before publish.
- Regression tests cover allowed trigger values, daemon `set`/`reactive`
  mapping, direct fallback, payload size and contract schema, and non-fatal
  publish failure.

## Acceptance

- [ ] A daemon converge job carries a trigger in `HISTORY_TRIGGERS`; `set`
      jobs record `set`, daemon reactions record `reactive`.
- [ ] `wallpaper.state` payloads use only contract stages and stay under
      `MAX_PAYLOAD_BYTES`; oversized optional fields are trimmed.
- [ ] Publish failure is non-fatal, warning-visible, and does not print a
      traceback that masks the result.
- [ ] Tests cover trigger validation, daemon `set`, reactive, direct fallback,
      payload bound, and non-fatal publish.
- [ ] `make gates` and `make gates-ci` pass; tag `v1.0.1`; fast-forward merge
      to `main`; push `main` + tag. Do **not** run `apply`.

## Evidence

Live failure reproduced from the report above (`dotfiles-runtime` v1.0.0,
daemon-owned bus). Code evidence:

- `src/runtime/cli/main.py` (`_daemon_converge_worker`) hardcodes
  `trigger="daemon"`.
- `src/runtime/application/reconcile.py:261` rejects any trigger outside
  `HISTORY_TRIGGERS`.
- `src/runtime/domain/history.py` is the canonical enum (no `daemon`).
- `src/runtime/cli/main.py` (`_publish_wallpaper_state`) forwards unbounded
  `error`/`reload_failures` and stages `converging`/`palette` that are absent
  from `contracts/event-contract.json` `topics['wallpaper.state'].enum.stage`.
- `src/runtime/adapters/emit_validation.py` caps payloads at 64 KiB and raises
  `PayloadTooLarge` for schema and size violations.
