---
id: REQ-013
title: "Emit skin consumer pointers for every skin target, not just AGS"
kind: requirement
status: done
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-013/
branch: ticket/REQ-013-skin-pointers-all-apps
---

# REQ-013 — Emit skin consumer pointers for every skin target, not just AGS

## Requirement

Live E2E on the installed v0.8.1 found that only the bar receives skin
pointers. The runtime emits `current/skins/<configDir>.css` for all seven AGS
targets, but `src/runtime/adapters/consumer_path_spec.py` hardcodes two
`ags`-only entries:

```text
config/ags/skin.css  -> skins/ags.css
config/ags/skin.json -> skin.json
```

The other six config dirs have no `skin.css` / `skin.json` pointers, so their
`createAppSkin({configDir})` no-ops and falls back. Contract v0.6.0 requires
`config/<configDir>/skin.{css,json}` for every target.

Required correction:

- Remove the hardcoded AGS skin pair from the static table.
- Derive `configDir`s from the skin artifacts the current generation produced
  (`<configDir>.css` plus `skin.json`); never hardcode an app name.
- For every derived `<configDir>`, ensure `<install>/config/<configDir>/skin.css`
  -> `current/skins/<configDir>.css` and
  `<install>/config/<configDir>/skin.json` -> `current/skin.json`, only when
  `<install>/config/<configDir>` exists. The runtime must never create an app
  config dir.
- Keep the atomic tmp-symlink + `os.replace` repoint behavior and the existing
  pointer style. Leave non-skin consumer pointers untouched.
- Idempotency holds; a missing config dir is skipped without an error (debug
  log, and omitted from inspect/doctor projections).
- Prune stale skin pointers when a generation no longer produces a target's
  `<configDir>.css` (decision recorded in `docs/ARCHITECTURE.md`): only
  runtime-owned skin symlinks under an existing config dir are removed; the
  pinned non-skin table is never pruned.
- Inspect and doctor report the derived pointers for every installed target.

## Acceptance

- [x] The static consumer table contains no skin entries and no app names.
- [x] Skin pointers are derived from `SkinEntry.artifact_hashes`; two or more
      config dirs receive both pointers.
- [x] An artifact whose config dir is absent is skipped; no directory is
      created and no error is raised.
- [x] A generation change repoints existing pointers to the new current/
      targets atomically.
- [x] Stale skin pointers for removed targets are pruned; non-skin pointers
      and unrelated files in a config dir are untouched.
- [x] The pointer set is proven data-derived (a non-AGS and an unknown config
      dir are handled generically).
- [x] `inspect status` and `doctor` report the derived pointers and do not
      report absent config dirs as drift.
- [x] The bar's existing `config/ags/skin.css` / `config/ags/skin.json`
      behavior is unchanged.
- [x] Release metadata is the next patch (`v0.8.2`); no existing tag is moved.
- [x] `make gates` and `make gates-ci` pass.

## Evidence

Baseline on the installed v0.8.1 (`dotfiles-runtime version` -> `0.8.1`),
2026-11-06:

```sh
ls -l ~/.local/state/dotfiles/current/skins
# ags.css, ags-btop-studio.css, ags-capture.css, ags-clipboard.css,
# ags-icme.css, ags-notifications.css, ags-wallpaper-selector.css (7 links)
```

```sh
for d in ~/.local/share/dotfiles/config/*; do
  printf '%s: ' "$(basename "$d")"
  ls -l "$d/skin.css" "$d/skin.json" 2>/dev/null | sed 's/.*-> //' | tr '\n' ' '
  echo
done
# ags: .../current/skins/ags.css .../current/skin.json
# ags-btop-studio:
# ags-capture:
# ags-clipboard:
# ags-icme:
# ags-notifications:
# ags-wallpaper-selector:
# (all other config dirs empty)
```

`~/.local/state/dotfiles/current.json` carries the seven CSS artifacts plus
`skin.json`; the pinned assets template tree has exactly the seven
`<configDir>.gtk.css.j2` files. The hardcoded `ags` pair is at
`src/runtime/adapters/consumer_path_spec.py:59-60`.

## Implementation evidence

Executed on the ticket worktree (2026-11-06):

```sh
uv run pytest -q tests/unit/test_skins.py tests/unit/test_seed_cache.py \
  tests/unit/test_inspect.py tests/unit/test_doctor_check.py \
  tests/unit/test_reconcile.py tests/architecture
# 313 passed
```

A live-shaped prototype (temp install spine + the seven real artifact names
from `~/.local/state/dotfiles/current.json`) produced both pointers for every
installed target and never created an absent dir:

```text
ags: skin.css -> .../current/skins/ags.css, skin.json -> .../current/skin.json
ags-clipboard: skin.css -> .../current/skins/ags-clipboard.css, ...
ags-notifications: ... ags-wallpaper-selector: ... ags-capture: ...
ags-icme: ... ags-btop-studio: ...
missing-dir-created: False
```

The pointer set is data-derived: the `custom-app` test creates
`config/custom-app/skin.{css,json}` from a `custom-app.css` artifact even
though the runtime has never heard of that app, and no `ags` pointer appears.
Stale pruning removes the runtime-owned `ags-clipboard` pair after a
generation drops its template while leaving `config/ags/colors.css`,
`notes.txt`, and a user symlink untouched.

```sh
make gates
# test       2250 passed, 3 skipped, 10 deselected
# lint       All checks passed
# contracts  48 passed, 1 skipped
# hermetic   2250 passed, 3 skipped, 10 deselected
# lock-check Resolved 25 packages
# docs-check exit 0
# make gates exit=0
```

```sh
make gates-ci
# same set and results as make gates; exit 0
```

Release metadata is `0.8.2` in `pyproject.toml`, `uv.lock` and
`runtime.__version__`; the annotated `v0.8.2` tag is created after the
fast-forward merge. No existing tag was moved. Nothing is pushed.

## Implementation map

- `src/runtime/domain/models.py` — `skin_config_dirs()` derives config dirs
  from `<configDir>.css` artifact keys.
- `src/runtime/ports/consumer_path_spec.py` — generic `skin_pointers()`
  naming rule (no app names).
- `src/runtime/adapters/consumer_path_spec.py` — static table keeps only the
  five palette/theme consumers.
- `src/runtime/adapters/seeder.py` — derive skin pointers from the skin
  entry, skip absent config dirs at debug level, prune stale runtime-owned
  skin pointers, preserve atomic repoint.
- `src/runtime/application/{inspect,doctor}.py` — report the derived pointer
  set and omit absent config dirs.
- `src/runtime/application/{seed_cache,reconcile,regenerate_icons}.py` — pass
  the active skin entry into the consumer repoint.
- Tests under `tests/unit/` cover two+ dirs, absent dir, repoint, pruning,
  data-derived set, inspect and doctor.
- `README.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md`, `pyproject.toml`,
  `uv.lock`, `src/runtime/__init__.py` — behavior + release metadata.
