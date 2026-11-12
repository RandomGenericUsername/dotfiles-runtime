---
id: BUG-020
title: "Kitty conf artifact keeps px suffixes: the conf renderer never applies the px→pt rule"
kind: bug
status: in-progress
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-020/
branch: ticket/BUG-020-kitty-conf-px-to-pt
repro: "Render the assets kitty template with the terminal-host tokens (`type.size-body = 12`, `surface.window.padding = 9`) through the runtime conf renderer, or read the live artifact: `kitty +runpy 'from kitty.config import load_config; b=[]; load_config(\"$XDG_STATE_HOME/dotfiles/current/appearance/kitty.conf\", accumulate_bad_lines=b); print(b)'`."
observed: "`current/appearance/kitty.conf` contains `font_size 12px` and `window_padding_width 9px`; kitty 0.50.0 reports two `BadLine(ValueError(\"could not convert string to float: '12px'\"))` / `'9px'` entries. The runtime `conf` path delegates to `_css_v2_value`, which appends `px` to every numeric length."
expected: "The `conf` renderer emits bare pt decimals for kitty lengths, using the docs/113 §7.3 rule: `value * 0.75`, round half up to two decimals, then clamp (`font_size` 6–36, `window_padding_width` 0–32), with trailing zeros stripped (`12 → 9`, `9 → 6.75`, `12.5 → 9.38`). `background_opacity` stays a bare unitless `[0,1]` number, `font_family` is the first usable family, and no generated `conf` line carries a `px` or `pt` suffix. CSS and rasi output stay byte-identical."
---

# Kitty conf artifact keeps px suffixes: the conf renderer never applies the px→pt rule

## Triage

- `kind: bug` — `skin_expander._format_v2_value` sends `conf` tokens through
  the CSS unit rule (`_css_v2_value`), so numeric lengths become `<n>px`.
  Kitty expects bare points and rejects the config line.
- `lane: 2` — bounded adapter fix plus regression tests; no OpenSpec change.
- `severity: high` — every generated `appearance/kitty.conf` has two bad lines
  and kitty silently falls back to its default font size/padding.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` returned 0 on main
  (`99c8034`) before `create BUG-020 kitty-conf-px-to-pt`.

## Evidence

- `Evidence:` live `~/.local/state/dotfiles/current/appearance/kitty.conf`
  (assets v0.19.0) contains `font_size 12px`, `window_padding_width 9px`,
  `background_opacity 1` (read 2026-11-11).
- `Evidence:` kitty 0.50.0 parses the live artifact with two bad lines:
  `[BadLine(number=15, line='font_size 12px', ...), BadLine(number=16,
  line='window_padding_width 9px', ...)]`.
- `Evidence:` the normative rule is docs/113 §7.3/§7.6
  (`dotfiles-v3/docs/113-window-role-kitty-projection.md:397-427,466-482`):
  `px * 0.75`, round half up to two decimals, clamps 6–36 / 0–32, bare
  decimals, no `px`/`pt`; opacity is unitless.
- `Evidence:` the runtime defect is the final `return _css_v2_value(path,
  value)` for `conf` in `_format_v2_value`
  (`src/runtime/adapters/skin_expander.py:401-418`); the CSS rule appends
  `px` at `:368`.
- `Evidence:` assets `skins/templates/kitty.conf.j2` uses bare placeholders
  `{{SKIN_TYPE_SIZE_BODY}}`, `{{SKIN_SURFACE_WINDOW_PADDING}}`,
  `{{SKIN_SURFACE_WINDOW_OPACITY}}` and must not append a unit.

## Repro

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime/.worktrees/BUG-020
kitty +runpy 'from kitty.config import load_config; b=[]; load_config("'"$HOME"'/.local/state/dotfiles/current/appearance/kitty.conf", accumulate_bad_lines=b); print(b)'
uv run python - <<'PY'
from pathlib import Path
from runtime.adapters.skin_expander import (
    expand_v2_template, load_skin_manifest, resolve_v2_token_document,
)
tokens = resolve_v2_token_document(
    load_skin_manifest(Path("tests/fixtures/skins-v2/manifests/terminal.toml")), "dark"
)
print(expand_v2_template(
    "font_size {{SKIN_TYPE_SIZE_BODY}}\n"
    "window_padding_width {{SKIN_SURFACE_WINDOW_PADDING}}\n",
    tokens,
    format="conf",
))
PY
```

## Observed

```text
[BadLine(number=15, line='font_size 12px', exception=ValueError("could not convert string to float: '12px'"), ...), BadLine(number=16, line='window_padding_width 9px', exception=ValueError("could not convert string to float: '9px'"), ...)]
font_size 12px
window_padding_width 9px
```

## Expected

- `conf` numeric length tokens render as bare pt decimals via
  `round_half_up(value * 0.75, 2)`, with `font_size` clamped to `6..36` and
  `window_padding_width` clamped to `0..32`; trailing zeros are stripped.
- `background_opacity` is the authored `surface.window.opacity` as a bare
  unitless `[0,1]` number; no alpha derivation.
- `font_family` is the first usable family (CSS-wide and non-portable system
  keywords dropped, quotes/escapes stripped, `monospace` fallback).
- No generated `conf` line has a `px` or `pt` suffix; no literal colour or
  paint token is emitted.
- CSS and rasi artifacts are byte-identical to `v1.5.0` output.

## Fix notes

Add a conf-only numeric policy in `skin_expander`; keep `_css_v2_value` and
`_rasi_value` untouched. Add the first-usable-family projection for
`type.font-family` only in the conf path. Cover terminal-host/terminal/paper
values, clamps, opacity, the no-suffix invariant, CSS/rasi byte identity, and a
`kitty +runpy` `load_config` probe with zero bad lines. No assets, composition,
provisioning, contract, app, live-state or apply change.

## Outcome (2026-11-11)

**Worktree:** `.worktrees/BUG-020/`
**Branch:** `ticket/BUG-020-kitty-conf-px-to-pt`
**Commits:** `20ddaef` (ticket + INDEX plan), `c7c6e84` (conf px→pt policy +
regression tests), this release commit (version metadata/CHANGELOG/README +
ticket close).
**Tag:** `v1.5.1` (annotated) after green gates.

### Exact conversion

| kitty option | token | rule | terminal-host | terminal | paper | glass |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `font_size` | `type.size-body` | `clamp(round_half_up(px * 0.75, 2), 6, 36)`, trailing zeros stripped | `12 → 9` | `12 → 9` | `13 → 9.75` | `13 → 9.75` |
| `window_padding_width` | `surface.window.padding` | `clamp(round_half_up(px * 0.75, 2), 0, 32)`, trailing zeros stripped | `9 → 6.75` | `9 → 6.75` | `11 → 8.25` | `11 → 8.25` |
| `background_opacity` | `surface.window.opacity` | bare unitless `[0,1]` | `1` | `1` | `1` | `0.94` |
| `font_family` | `type.font-family` | first usable family | `JetBrainsMono Nerd Font` | `JetBrainsMono Nerd Font` | `Cantarell` | `monospace` |

`round_half_up` is decimal half-up, so `12.5px → 9.38`; the clamp is applied
after rounding. No generated `conf` line contains `px` or `pt`.

### Delivered

| Path | What changed |
| --- | --- |
| `src/runtime/adapters/skin_expander.py` | `conf`-only `_conf_value`/`_conf_pt_value`/`_first_usable_font_family`; `_format_v2_value` routes `conf` away from `_css_v2_value`; CSS/rasi/theme paths untouched |
| `tests/unit/test_kitty_conf_projection.py` | exact terminal-host/terminal/paper/glass values, clamps/half-up, unitless opacity, first-family, no `px`/`pt`, CSS/rasi byte identity, paint fail-closed, live `kitty +runpy` zero bad lines (explicit skip without kitty) |
| `tests/fixtures/skins-v2/manifests/terminal-host.toml` | byte copy of the assets v0.19.0 terminal-host manifest for the exact-value test |
| `pyproject.toml` / `src/runtime/__init__.py` / `uv.lock` / `CHANGELOG.md` / `README.md` | `1.5.1` release metadata and notes |

### Tests

```sh
uv run pytest -q tests/unit/test_kitty_conf_projection.py \
  tests/unit/test_format_aware_artifacts.py \
  tests/unit/test_appearance_v2_assets.py tests/unit/test_skins.py \
  tests/unit/test_host_defaults.py
# 156 passed in 3.52s
```

### Gates

```sh
make gates
# test: 2592 passed, 3 skipped, 11 deselected
# lint: All checks passed
# contracts-check: 58 passed, 1 skipped
# hermetic: 2592 passed, 3 skipped, 11 deselected
# lock-check: Resolved 25 packages
# docs-check: rc 0

make gates-ci
# same six gates: 2592 passed, 3 skipped, 11 deselected

make ci-gates-parity
# PASS gates=['contracts-check','docs-check','hermetic','lint','lock-check','test']
# jobs=[...,'release'] exempt=['ci-gates-parity','release']
```

### Downstream pin needed

- **`dotfiles-runtime v1.5.1`** — the composition pin must move here for the
  generated `appearance/kitty.conf` to become `font_size 9` /
  `window_padding_width 6.75` instead of the rejected `12px`/`9px`.
- No assets, provisioning, contract, app, composition or live-state edit was
  made, and no `apply` was run. The live artifact remains the old bytes until
  the composition pin is bumped and the generation is regenerated.
