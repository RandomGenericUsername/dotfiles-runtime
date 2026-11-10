---
id: BUG-018
title: "`icons regenerate` commits a new generation when the content is already current"
kind: bug
status: in-progress
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-018/
branch: ticket/BUG-018-icons-regenerate-idempotent
repro: "With a current generation whose content layers are all unchanged, run `icons regenerate` (or `uv run python /tmp/opencode/repro_bug018.py`): the use case commits a second generation, moves `current`, appends a history line and reloads consumers even though only operational manifest fields (source intent/kind/jobId, timestamps) differ."
observed: "Live: generation `43d62e17…` became `d4d00d59…` after `icons regenerate`; the manifest diff showed only `committed`/`created` timestamps, `source.intentHash`/`source.kind`/`jobId`, while palette/wallpaper/effects/icons/skin/appearance were identical. Prototype output: `before: 28f2dc268222…`, `after: 859d9bf468e5…`, `new generation: True`, `cache_hit: True`, `history lines: 1 -> 2`, `reload calls: 2`."
expected: "When the resolved generation content equals `current`, `icons regenerate` is a strict no-op: `current`/`current.json` stay byte-identical, no new generation directory and no history line are written, and no AGS/bar refresh or reloader runs. Changing the icons layer (or any content input) still commits normally."
---

# `icons regenerate` commits a new generation when the content is already current

## Triage

- `kind: bug` — the GUI live-toggle path is not idempotent: an unchanged
  icons cache hit still mutates state and restarts consumers.
- `lane: 2` — bounded application/adaptor identity fix plus regression tests;
  no OpenSpec change.
- `severity: high` — every toggle writes a generation, moves `current`,
  appends history and restarts the bar, creating avoidable churn and visible
  flashing.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` returned 0 on main
  (`a0f6347`) before `create BUG-018 icons-regenerate-idempotent`.

## Evidence

- `Evidence:` `RegenerateIconsUseCase.run` always calls reconcile with
  `force_icons=True` and no idempotent short-circuit:
  `src/runtime/application/regenerate_icons.py:122-149`.
- `Evidence:` reconcile always commits a generation, saves `current.json`,
  appends history, and refreshes AGS/reloaders:
  `src/runtime/application/reconcile.py:449-612`, `:651-674`.
- `Evidence:` generation identity hashes `spec.intent_hash`:
  `src/runtime/adapters/generation_store.py:183-207`, `:325-333`.
  A caller-supplied operational intent hash therefore changes the
  generation hash even when every content layer is identical.
- `Evidence:` `GenerationStore.commit` always calls `_commit_pointer`, even
  when the immutable directory already exists:
  `src/runtime/adapters/generation_store.py:337-368`.
- `Evidence:` live manifest diff supplied with the report: generation
  `43d62e17…` → `d4d00d59…`, only timestamps and
  `source.intentHash`/`source.kind`/`jobId` changed; all layer hashes,
  skin and appearance targets were identical.

## Repro

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime/.worktrees/BUG-018
PYTHONPATH=. uv run python /tmp/opencode/repro_bug018.py
```

## Observed

```text
reconcile: effects layer is null; consumer symlink skipped
reconcile: effects layer is null; consumer symlink skipped
before: 28f2dc268222441d781a4bbbe6e893232550eb8631cb4120d95e05e5a4224452
after:  859d9bf468e58d5530d7e8e5fc1b78371bfeb288b0f6d8e6d486732a532d2dfa
new generation: True
cache_hit: True no_op: n/a
history lines: 1 -> 2
reload calls: 2
```

## Expected

- A generation whose content identity (layer hashes + skin/mode + composed
  appearance + host) equals `current` produces no new generation directory,
  no `current` move, no `current.json` write and no history line.
- `icons regenerate` returns `cache_hit=True`, `no_op=True`, and the current
  icons entry; no AGS/bar refresh or reloader runs.
- Operational manifest metadata (`source.kind`, `jobId`, timestamps and a
  caller-supplied operational intent hash) is never part of generation
  identity.
- Changed icons (or any changed generation input) keep the normal
  commit → `current.json` → history → refresh path.
- Existing regenerate/converge/GC/doctor behaviour and the one-lock rule are
  preserved.

## Fix notes

Normalize generation identity to the content intent (ignore the
caller-supplied operational intent hash) and make `GenerationStore.commit`
reuse `current` when its manifest resolves to the same content identity.
`icons regenerate` opts into the strict unchanged short-circuit; normal
reconcile keeps its history/apply semantics but also avoids re-creating an
identical generation. No-op runs append no history line and never touch the
appearance ledger.
