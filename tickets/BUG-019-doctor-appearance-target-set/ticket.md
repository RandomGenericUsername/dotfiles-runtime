---
id: BUG-019
title: "Doctor flags legitimate stable appearance.css links whose consumer alias differs from the config dir name"
kind: bug
status: in-progress
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-019/
branch: ticket/BUG-019-doctor-appearance-target-set
repro: "On the live post-Phase-3A-4 host: `ls -l ~/.local/share/dotfiles/config/gtk-3.0/appearance.css ~/.local/share/dotfiles/config/gtk-4.0/appearance.css` then `uv run dotfiles-runtime doctor -f json`; the two links point at `state/current/appearance/gtk3.css` / `gtk4.css`, which are committed `appearance.targets`, yet doctor reports two diverged consumer rows."
observed: "`doctor -f json` exit 1: `diverged | consumer/config/gtk-3.0/appearance.css | retired v2 convenience link: .../config/gtk-3.0/appearance.css; v3 apps read $XDG_STATE_HOME/dotfiles/current/appearance/gtk-3.0.css` and the same for `gtk-4.0`. Both links resolve to `state/current/appearance/gtk3.css` / `gtk4.css`, and the committed generation's `appearance.targets` contains `{configDir: gtk3, path: appearance/gtk3.css}` and `{configDir: gtk4, path: appearance/gtk4.css}`."
expected: "A real config dir's `appearance.css` is clean when its lexically normalized raw target is exactly `<state_root>/current/appearance/<name>.css`, that target exists through the `current` hop, and `<name>.css` is a path in the committed generation's `appearance.targets` (or the matching committed `appearance.artifacts` for non-CSS formats). The manifest target/artifact set is the only alias authority: no gtk/wlogout alias table. Dangling, wrong-target, generation-specific, app-tag and old-v2 cache targets still diverge; the BUG-016 wlogout stable link, AGS config-symlink skip and `skin.css`/`skin.json` retired-pointer checks stay unchanged."
---

# Doctor flags legitimate stable appearance.css links whose consumer alias differs from the config dir name

## Triage

- `kind: bug` — the BUG-016 rule derives the expected target name from the
  config dir (`gtk-3.0.css`, `gtk-4.0.css`), but the committed generation
  publishes the consumer aliases `gtk3.css` / `gtk4.css`; a correct
  provisioning-owned stable link is reported as retired v2 drift.
- `lane: 2` — bounded application-layer fix plus regression tests; no
  OpenSpec change.
- `severity: high` — a clean post-Phase-3A-4 desktop always reports drift, so
  `doctor` cannot be used as the convergence signal.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` returned 0 on main
  (`43595a2`) before `create BUG-019 doctor-appearance-target-set`.

## Evidence

- `Evidence:` live links `~/.local/share/dotfiles/config/gtk-3.0/appearance.css
  -> ~/.local/state/dotfiles/current/appearance/gtk3.css` and
  `gtk-4.0/appearance.css -> .../current/appearance/gtk4.css`
  (`ls -l`, 2026-11-11).
- `Evidence:` the live committed manifest target set is
  `[{configDir: gtk3, path: appearance/gtk3.css},
  {configDir: gtk4, path: appearance/gtk4.css}, ..., {configDir: wlogout,
  path: appearance/wlogout.css}]`; the live `rofi` artifact is
  `{consumer: rofi, format: rasi, path: appearance/rofi.rasi}`
  (`~/.local/state/dotfiles/current/manifest.json`).
- `Evidence:` `_is_stable_appearance_link` compares the link target against
  `state_root / "current" / "appearance" / f"{link.parent.name}.css"`
  (`src/runtime/application/doctor.py:94-114`), so `gtk-3.0` can never match
  `gtk3.css`.
- `Evidence:` `GenerationStore` publishes exactly those target/artifact
  paths in the manifest (`src/runtime/adapters/generation_store.py:584-597`,
  `:660-675`) and `verify()` requires each path to exist
  (`src/runtime/adapters/generation_store.py:809-825`).

## Repro

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime/.worktrees/BUG-019
ls -l ~/.local/share/dotfiles/config/gtk-3.0/appearance.css \
      ~/.local/share/dotfiles/config/gtk-4.0/appearance.css
uv run dotfiles-runtime doctor -f json | python3 -m json.tool
```

## Observed

```text
.../config/gtk-3.0/appearance.css -> .../current/appearance/gtk3.css
.../config/gtk-4.0/appearance.css -> .../current/appearance/gtk4.css
diverged | consumer/config/gtk-3.0/appearance.css | retired v2 convenience link: ...; v3 apps read $XDG_STATE_HOME/dotfiles/current/appearance/gtk-3.0.css
diverged | consumer/config/gtk-4.0/appearance.css | retired v2 convenience link: ...; v3 apps read $XDG_STATE_HOME/dotfiles/current/appearance/gtk-4.0.css
```

## Expected

- A real config dir's `appearance.css` is clean when its normalized raw
  target is exactly `<state_root>/current/appearance/<name>.css`, the target
  exists, and `<name>.css` is present in the committed generation's
  `appearance.targets` paths (non-CSS formats: the matching committed
  `appearance.artifacts`).
- The committed manifest target/artifact set drives acceptance; no
  config-dir-to-alias table is introduced.
- Generation-specific paths, app-tag paths, old v2 cache targets, wrong
  targets, dangling targets and names absent from the committed set still
  report `diverged | retired v2 convenience link`.
- A config dir that is itself a symlink (AGS app tag) is still skipped, and
  `skin.css` / `skin.json` still report retired v1 skin pointers.

## Fix notes

Pass the verified committed manifest's allowed appearance names into the
stable-link predicate in `_check_appearance`; keep the lexical normalization
and the `current` hop non-resolved. Add regression tests for gtk3/gtk4 alias
links and for the manifest-driven acceptance (a name is accepted only when
the committed target set contains it). No provisioning, live-state or
downstream edit.
