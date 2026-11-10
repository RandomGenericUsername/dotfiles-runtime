---
id: BUG-017
title: "Runtime appearance prune deletes the provisioning-owned wlogout stable link"
kind: bug
status: done
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-017/
branch: ticket/BUG-017-appearance-consumer-ownership
repro: "Create a temp state/spine with real `config/ags` and `config/wlogout` dirs, each holding `appearance.css -> current/appearance/<name>.css`, then call `prune_stale_appearance_consumers(spine, {spine/config/ags/appearance.css})`; the wlogout link is unlinked. Live equivalent: `ls -l ~/.local/share/dotfiles/config/wlogout/appearance.css` after a runtime skin converge."
observed: "`prune_stale_appearance_consumers` unlinks every real `config/*/appearance.css` symlink whose raw target names `current/appearance` and is not in `wanted`; `wanted` contains only the runtime's published appearance targets. The v3 converge cleanup (`CacheSeeder.prune_runtime_spine_pointers`) likewise unlinks the provisioning-owned `config/wlogout/appearance.css -> current/appearance/wlogout.css`. Live wlogout then fails its `@import \"appearance.css\"` while `doctor` stays clean."
expected: "The prune is ownership-aware: only AGS appearance consumers the runtime publishes are pruned. A provisioning-owned non-AGS stable link such as `config/wlogout/appearance.css -> current/appearance/wlogout.css` survives seed/converge. The narrow `current/appearance` target check, symlink-only scope, and AGS retirement cleanup remain. No app name is hardcoded."
---

# Runtime appearance prune deletes the provisioning-owned wlogout stable link

## Triage

- `kind: bug` — the final SKIN-WLOGOUT cutover made provisioning own
  `config/wlogout/appearance.css -> current/appearance/wlogout.css`; runtime
  appearance cleanup still treats it as a stale runtime convenience link.
- `lane: 2` — bounded adapter fix plus regression tests; no OpenSpec change.
- `severity: high` — a skin/mode converge silently breaks the live wlogout
  stylesheet while `doctor` reports clean, hiding the regression.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` returned 0 on main
  (`2ab478a`) before `create BUG-017 appearance-consumer-ownership`.

## Evidence

- `Evidence:` current prune scope is every real config dir:
  `src/runtime/adapters/appearance.py:563-590` skips only `link in wanted`,
  non-symlinks and non-`current/appearance` targets.
- `Evidence:` `CacheSeeder.publish_appearance` passes only the published
  target paths as `wanted`: `src/runtime/adapters/seeder.py:793-805`.
- `Evidence:` v3 converge runs `prune_runtime_spine_pointers` on every commit
  and also unlinks `config/*/appearance.css` links naming
  `current/appearance` (`src/runtime/application/reconcile.py:486-490`,
  `src/runtime/adapters/seeder.py:911-951`), so the live reconverge path
  deletes wlogout even when `publish_appearance` is not on that path.
- `Evidence:` provisioning owns the stable wlogout link:
  `dotfiles-provision/ansible/roles/state_links/vars/main.yml:30-39`,
  `dotfiles-provision/tickets/REQ-033-skin-wlogout/ticket.md:20-35`.
- `Evidence:` live `~/.local/state/dotfiles/current/manifest.json` includes
  `{"configDir": "wlogout", "path": "appearance/wlogout.css"}`, but
  `~/.local/share/dotfiles/config/wlogout/appearance.css` is currently absent.
- `Evidence:` prototype repro (`uv run python /tmp/opencode/repro_bug017.py`,
  exit 0):
  `legacy prune removed: ['wlogout']` / `wlogout after legacy prune: False`
  and `wlogout after v3 prune: False`.

## Repro

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime/.worktrees/BUG-017
uv run python /tmp/opencode/repro_bug017.py
```

## Observed

```text
legacy prune removed: ['wlogout']
wlogout after legacy prune: False
v3 converge prune removed: ['ags']
wlogout after v3 prune: False
```

## Expected

- Appearance cleanup is scoped to runtime-published AGS consumers, derived from
  the appearance target set (`appearance.config_dirs`) filtered by the
  repo-wide AGS config-dir identity (`ags`, `ags-*`) — no `wlogout` constant.
- `config/wlogout/appearance.css -> current/appearance/wlogout.css` survives
  both the legacy publish prune and the v3 converge prune across a skin/mode
  reconverge.
- Real AGS-like dirs with stale `current/appearance` links are still pruned;
  config dirs that are themselves symlinks are skipped; non-`current/appearance`
  links and regular files are preserved.
- `doctor` remains clean for the stable wlogout link (BUG-016 behaviour).
- No `.seed.lock` is introduced; state mutation still uses the one
  `converge.lock`.

## Outcome (2026-11-10)

**Worktree:** `.worktrees/BUG-017/`
**Branch:** `ticket/BUG-017-appearance-consumer-ownership`
**Commits:** `56c4bb1` (ticket + INDEX plan), `5e22784` (fix + regression
tests), this release commit (version/CHANGELOG/README + ticket close).
**Tag:** `v1.1.1` (annotated) after green gates.

### Exact prune change

The appearance adapter now derives the runtime-owned consumer names from the
published appearance target set (`appearance.config_dirs`) using the repo-wide
AGS config-dir identity (`ags`, `ags-*`). Both `prune_stale_appearance_consumers`
and `CacheSeeder.prune_runtime_spine_pointers` prune `appearance.css` only for
those owned names. Reconcile passes the committed appearance's
`config_dirs` into the v3 cleanup. The existing rules stay: symlink-only,
raw target must name `current/appearance`, and AGS links with a stale target
still retire. No `wlogout` constant was added.

### Delivered

| Path | What changed |
| --- | --- |
| `src/runtime/adapters/appearance.py` | `is_ags_appearance_consumer`, `runtime_appearance_consumer_names`; ownership-aware `prune_stale_appearance_consumers` |
| `src/runtime/adapters/seeder.py` | `publish_appearance` passes `published`; `prune_runtime_spine_pointers` applies the same ownership filter |
| `src/runtime/application/reconcile.py` | passes the committed `appearance_entry.config_dirs` to the converge prune |
| `tests/unit/test_appearance.py` | wlogout-like stable survival, AGS stale prune, symlink-dir skip, non-current target preservation |
| `tests/unit/test_wlogout_skin_consumer.py` | wlogout link survives a dark→light appearance reconverge + doctor consumer row absent |
| `tests/unit/test_reconcile.py` | full v3 reconcile keeps a non-AGS stable appearance link |
| `pyproject.toml` / `src/runtime/__init__.py` / `uv.lock` / `CHANGELOG.md` / `README.md` | `1.1.1` release metadata and notes |

### Targeted tests

```sh
uv run pytest -q tests/unit/test_appearance.py \
  tests/unit/test_appearance_v2_assets.py \
  tests/unit/test_wlogout_skin_consumer.py \
  tests/unit/test_seed_cache.py tests/unit/test_generations_v3.py \
  tests/unit/test_reconcile.py tests/unit/test_doctor_check.py \
  tests/unit/test_doctor_repair.py tests/unit/test_one_lock.py
# 260 passed
```

### Gates

```sh
make gates
# test: 2492 passed, 3 skipped, 11 deselected
# lint: All checks passed
# contracts-check: 58 passed, 1 skipped
# hermetic: 2492 passed, 3 skipped, 11 deselected
# lock-check: Resolved 25 packages
# docs-check: rc 0

make gates-ci
# same six gates, all green
make ci-gates-parity
# PASS gates=[...] jobs=[...] exempt=['ci-gates-parity', 'release']
```

### Live read-only confirmation

The live wlogout link is currently absent (the bug already fired), so the
survival proof is the dark→light reconverge test above:
`config/wlogout/appearance.css` keeps the exact raw target
`current/appearance/wlogout.css` across two appearance generations and the v3
cleanup, and doctor reports no
`consumer/config/wlogout/appearance.css` row. No `apply` was run and no live
state, provisioning, composition, app or contract content was changed.

## Fix notes

Centralize the ownership predicate in the appearance adapter and use it from
both prune paths. Keep the existing narrow target check and symlink scope.
Regression tests cover wlogout survival through a skin/mode reconverge, AGS
stale pruning, symlink-dir skip, non-current target preservation, doctor
cleanliness and the one-lock invariant.
