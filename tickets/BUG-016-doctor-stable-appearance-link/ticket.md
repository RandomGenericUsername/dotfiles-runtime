---
id: BUG-016
title: "Doctor flags the legitimate stable wlogout appearance.css link as a retired v2 convenience link"
kind: bug
status: done
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-016/
branch: ticket/BUG-016-doctor-stable-appearance-link
repro: "`dotfiles-runtime doctor` on the live post-SKIN-WLOGOUT host (or `ls -l ~/.local/share/dotfiles/config/wlogout/appearance.css` plus the doctor consumer row)."
observed: "`doctor` reports `diverged | consumer/config/wlogout/appearance.css | retired v2 convenience link: /home/inumaki/.local/share/dotfiles/config/wlogout/appearance.css; v3 apps read $XDG_STATE_HOME/dotfiles/current/appearance/wlogout.css`, even though the link resolves to `~/.local/state/dotfiles/current/appearance/wlogout.css`, the provisioning-owned stable appearance link for a real (non-symlink) config dir."
expected: "For a real config dir, `appearance.css` is legitimate when it resolves exactly to `<state_root>/current/appearance/<config_dir.name>.css` and that target exists; only links resolving elsewhere (generation-specific path, app tag, old v2 target, dangling, wrong basename) are retired-drift. `skin.css`/`skin.json` retired-pointer checks and the AGS app-symlink skip stay unchanged."
---

# Doctor flags the legitimate stable wlogout appearance.css link as a retired v2 convenience link

## Triage

- `kind: bug` — the SKIN-WLOGOUT cutover made provisioning create a
  `state_links` `appearance.css` link for the real `wlogout` config dir; the
  doctor still classifies every real config dir's `appearance.css` symlink as
  retired v2 drift.
- `lane: 2` — bounded application-layer fix plus regression tests; no
  OpenSpec change.
- `severity: high` — a clean, legitimate desktop reports perpetual drift, so
  `doctor` cannot be used as the post-cutover signal and an operator may
  "repair" a provisioning-owned link.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` returned 0 on main
  (`ee6f0a7`) before `create BUG-016 doctor-stable-appearance-link`.

## Evidence

- `Evidence:` live link `~/.local/share/dotfiles/config/wlogout/appearance.css`
  is an absolute symlink to
  `/home/inumaki/.local/state/dotfiles/current/appearance/wlogout.css`; the
  resolved file exists under
  `~/.local/state/dotfiles/generations/6de6e687…/appearance/wlogout.css`.
  `readlink -f` returns that generation file, proving the stable `current`
  hop is live.
- `Evidence:` `config/ags` and the other AGS app config dirs are symlinks and
  are correctly skipped by `if config_dir.is_symlink(): continue`
  (`src/runtime/application/doctor.py:532-534`).
- `Evidence:` `_check_appearance` currently appends a retired-v2 drift item
  for **any** `appearance.css` symlink in a real config dir, without inspecting
  the link target (`src/runtime/application/doctor.py:540-552`).
- `Evidence:` provisioning creates this stable link for every real config
  dir; the runtime defines the canonical target as
  `current/appearance/<configDir>.css`
  (`src/runtime/ports/consumer_path_spec.py:35-55`,
  `src/runtime/adapters/seeder.py:773-805`).

## Repro

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
readlink ~/.local/share/dotfiles/config/wlogout/appearance.css
dotfiles-runtime doctor
```

## Observed

```text
/home/inumaki/.local/state/dotfiles/current/appearance/wlogout.css
...
diverged | consumer/config/wlogout/appearance.css | retired v2 convenience link: /home/inumaki/.local/share/dotfiles/config/wlogout/appearance.css; v3 apps read $XDG_STATE_HOME/dotfiles/current/appearance/wlogout.css
```

## Expected

- A real config dir's `appearance.css` symlink is clean when its normalized
  link target is exactly `<state_root>/current/appearance/<config_dir.name>.css`
  and that target exists (following the `current` generation hop).
- The same link is retired drift when it points at a generation-specific
  path, an app tag, another v2 target, a dangling path, or a different
  basename than the config dir.
- A config dir that is itself a symlink (AGS app tag) is still skipped
  entirely.
- `skin.css`/`skin.json` symlinks in real config dirs still report retired v1
  skin drift.

## Fix notes

Replace the unconditional flag with one generic rule in
`_check_appearance`: compute the stable target
`state_root / "current" / "appearance" / f"{config_dir.name}.css"`, compare it
to the link target lexically after `normpath` (so a stable link is not
mistaken for a generation path, and `/current/` is not resolved away), and
require `.exists()` for the target. Anything else is drift. No app-name
hardcoding, no provisioning or live-state edits.

## Outcome (2026-11-09)

**Worktree:** `.worktrees/BUG-016/`
**Branch:** `ticket/BUG-016-doctor-stable-appearance-link`
**Commits:** `8106196` (ticket + INDEX plan), `2882c19` (fix + regression
tests), this release commit (version/CHANGELOG/README + ticket close).
**Tag:** `v1.0.8` (annotated) after green gates.

### Exact rule

For each real (non-symlink) `config/*` dir, `appearance.css` is clean only
when `_is_stable_appearance_link` holds:

- the raw link target (absolute, or relative to the link's parent) is
  lexically normalized with `normpath`/`abspath` — the `current` hop is
  deliberately **not** resolved;
- the normalized target equals
  `<state_root>/current/appearance/<config_dir.name>.css`; and
- that stable target exists through the `current` generation hop.

Everything else is `diverged | retired v2 convenience link`: generation
paths, app-tag paths, old v2 cache targets, wrong basenames and dangling
stable paths. A config dir that is itself a symlink is still skipped before
the rule runs, and `skin.css`/`skin.json` still report retired v1 skin
pointers.

### Delivered

| Path | What changed |
| --- | --- |
| `src/runtime/application/doctor.py` | `_normalized_link_target`, `_stable_appearance_link_target`, `_is_stable_appearance_link`; `_check_appearance` uses them for `appearance.css` |
| `tests/unit/test_doctor_check.py` | `TestStableAppearanceConsumerLink`: live-shaped absolute and relative wlogout links clean; wrong target, dangling, generation path, app-tag path, wrong basename and v2 cache target drill; AGS config symlink still skipped |
| `pyproject.toml` / `src/runtime/__init__.py` / `uv.lock` / `CHANGELOG.md` / `README.md` | `1.0.8` release metadata and notes |

### Tests

```sh
uv run pytest -q tests/unit/test_doctor_check.py \
  tests/unit/test_doctor_repair.py tests/unit/test_appearance.py \
  tests/unit/test_skins.py tests/unit/test_appearance_v2_assets.py \
  tests/unit/test_generations_v3.py
# 164 passed in 5.56s
```

### Gates

```sh
make gates
# test: 2448 passed, 3 skipped, 11 deselected
# lint: All checks passed
# contracts-check: 58 passed, 1 skipped
# hermetic: 2448 passed, 3 skipped, 11 deselected
# lock-check: Resolved 25 packages
# docs-check: rc 0

make gates-ci
# same six gates: 2448 passed, 3 skipped, 11 deselected
make ci-gates-parity
# PASS gates=[...] jobs=[...] exempt=['ci-gates-parity', 'release']
```

### Live read-only check (fixed worktree)

```sh
uv run dotfiles-runtime doctor -f plain
# exit=0
# desktop clean: 27 item(s) checked; watch health: ok; ...
# no `consumer/config/wlogout/appearance.css` row
```

The live link
`~/.local/share/dotfiles/config/wlogout/appearance.css ->
~/.local/state/dotfiles/current/appearance/wlogout.css` is now accepted.
No `apply` was run and no live state, provisioning, composition, app or
contract content was changed.

