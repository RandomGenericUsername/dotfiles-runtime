---
id: BUG-018
title: "`icons regenerate` commits a new generation when the content is already current"
kind: bug
status: done
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

## Outcome (2026-11-10)

**Worktree:** `.worktrees/BUG-018/`
**Branch:** `ticket/BUG-018-icons-regenerate-idempotent`
**Commits:** `71ff322` (ticket + INDEX plan), `1e306e5` (fix + regression
tests), this release commit (version/CHANGELOG/README + ticket close).
**Tag:** `v1.1.2` (annotated) after green gates.

### Exact idempotency rule

- Generation identity is
  `compute_generation_content_hash(layerHashes[wallpaper, palette, effects?,
  icons?], skinName, skinHash, mode, appearanceHash, hostHash?)`; its intent
  component is recomputed from those content inputs. `source.kind`, `jobId`,
  `created`/`committed` timestamps and any caller-supplied operational/queue
  intent hash are **not** identity.
- `GenerationStore.commit` recomputes the content identity of the verified
  `current` manifest and returns `unchanged=True` (same `current` hash, no
  staging, no pointer move) when it equals the prospective identity. A
  legacy current generation with a different stored intent hash is still
  recognised as the same content.
- `icons regenerate` opts into `idempotent=True` at the reconcile boundary.
  On `generation.unchanged` it returns `cache_hit=True`, `no_op=True` and the
  current icons entry **without** saving `current.json`, appending history,
  running GC, touching the appearance ledger, refreshing AGS/bar or running
  reloaders. A no-op therefore records no history line.
- Any changed content input (notably a changed icons entry) misses the
  equality check and follows the normal commit → `current.json` → history →
  refresh path.

### Delivered

| Path | What changed |
| --- | --- |
| `src/runtime/adapters/generation_store.py` | content-only `compute_generation_content_hash`; `GenerationResult.unchanged`; verified `current` reuse in `commit`; manifest content recompute ignoring operational fields |
| `src/runtime/application/reconcile.py` | `idempotent` opt-in, `ReconcileResult.no_op`, strict unchanged short-circuit, force-icons hash assertion only on the ordinary path |
| `src/runtime/application/regenerate_icons.py` | passes `idempotent=True`, exposes `RegenerateIconsResult.no_op`, documents no-history no-op |
| `src/runtime/cli/main.py` | renders `icons unchanged: … current generation kept` + JSON `no_op` |
| `tests/unit/test_regenerate_icons.py` | cache-hit no-op byte-identity/no history/no reload; changed icons still commits |
| `tests/unit/test_generations_v3.py` | identity equality ignores operational source metadata; changed icons changes identity |
| `tests/unit/test_cli_icons.py` | no-op CLI rendering/JSON |
| `pyproject.toml` / `src/runtime/__init__.py` / `uv.lock` / `CHANGELOG.md` / `README.md` | `1.1.2` release metadata and notes |

### Targeted tests

```sh
uv run pytest -q tests/unit/test_regenerate_icons.py tests/unit/test_generations_v3.py \
  tests/unit/test_generation_gc.py tests/unit/test_reconcile.py \
  tests/unit/test_doctor_check.py tests/unit/test_one_lock.py tests/unit/test_cli_icons.py
# 150 passed

uv run pytest -q tests/unit/test_regenerate_icons.py tests/unit/test_regenerate_stale.py \
  tests/unit/test_cli_icons.py tests/unit/test_generations_v3.py \
  tests/unit/test_generation_gc.py tests/unit/test_reconcile.py \
  tests/unit/test_doctor_check.py tests/unit/test_doctor_repair.py \
  tests/unit/test_one_lock.py tests/unit/test_wlogout_skin_consumer.py \
  tests/unit/test_appearance.py tests/unit/test_host_defaults.py \
  tests/unit/test_converge_job_trigger_payload.py
# 277 passed
```

### Gates

```sh
make gates
# test: 2496 passed, 3 skipped, 11 deselected
# lint: All checks passed
# contracts-check: 58 passed, 1 skipped
# hermetic: 2496 passed, 3 skipped, 11 deselected
# lock-check: Resolved 25 packages
# docs-check: rc 0

make gates-ci
# same six gates, rc 0
make ci-gates-parity
# PASS gates=[...] jobs=[...] exempt=['ci-gates-parity', 'release']
```

### Live read-only confirmation

```console
$ uv run python - <<'PY'  # read-only over ~/.local/state/dotfiles
# current = GenerationStore(root).current_hash(); store.current_manifest()
PY
live current generation: fb8392f6ed52c661bc6c1d5c11903021f6177bb9d48d4d32e3f6990d11811f6f
source metadata: {'intentHash': '8665de40…', 'jobId': '3ce8e187…', 'kind': 'daemon'}
content identity:    fb8392f6ed52c661bc6c1d5c11903021f6177bb9d48d4d32e3f6990d11811f6f
content-identical after ignoring source/timestamps: True
```

The live generation's content identity is reproducible from its manifest
alone; the new commit would treat it as `unchanged`. No `apply`, generation
commit, pointer move or downstream edit was performed.
