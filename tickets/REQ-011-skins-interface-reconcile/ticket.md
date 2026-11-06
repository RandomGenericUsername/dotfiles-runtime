---
id: REQ-011
title: "Reconcile the skin expander to the canonical skin interface"
kind: requirement
status: in-progress
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-011/
branch: ticket/REQ-011-skins-interface-reconcile
---

# REQ-011 — Reconcile the skin expander to the canonical skin interface

## Requirement

REQ-010 invented a `version` / `[tokens]` / `[apps]` manifest format that does
not exist in the frozen contract or in the real assets. The shipped expander
therefore rejects every real skin manifest. Reconcile the runtime to the
canonical skin interface below without inventing new fields.

### Canonical manifest

One TOML/JSON manifest per skin under `skins/manifests/`:

- `schema_major = 1`
- `name = "<skin>"`
- exactly six token groups: `[shape]`, `[surface]`, `[ink]`, `[type]`,
  `[rhythm]`, `[layout]`
- numeric tokens are numbers (canonical, unitless): `radius-panel`,
  `radius-card`, `radius-cell`, `radius-pill`, `border-w`, `gap`,
  `title-size`, `text-size`, `panel-width`, `bt-list-spacing`,
  `bar-row-height`, `chip-min`
- string tokens: `cell-pad` (`"Npx Npx"`), the surface paint expressions
  (`bg-panel`, `bg-card`, `border`, `shadow`, `blur`), `ink`, `ink-dim`,
  `accent`, `ink-over`, `font-family`
- no `[tokens]`, no `[apps]`, no structure/variant keys.

The current assets manifest encodes dimensions as `"12px"` strings (the frozen
v1 assets schema). The expander accepts that dimension spelling for
compatibility and normalizes every dimension token to a raw number in
`skin.json`; CSS still receives `<number>px`. New manifests use unitless
numbers.

### Canonical template set + rendering

- One template per target config dir, `<configDir>.gtk.css.j2` under
  `skins/templates/`; the expander discovers every `*.gtk.css.j2` and writes
  `<configDir>.css`.
- `{{SKIN_<TOKEN>}}` where `<TOKEN>` is the token name uppercased with
  non-alphanumerics replaced by `_` (`radius-panel` → `SKIN_RADIUS_PANEL`).
  Unknown or unresolved placeholders fail closed.
- In CSS, numeric tokens render as `<number>px` (`12` → `12px`, `1.5` →
  `1.5px`, including layout tokens); `cell-pad` and all other string tokens
  render verbatim.
- `skin.json` is the grouped map
  `{"version": 1, "<group>": {"<token>": <raw number|string>}}` accepted by
  `ags-platform` `parseSkinJson`.

### Outputs/pointers (unchanged from REQ-010)

- `current/skins/<configDir>.css` per rendered template and
  `current/skin.json` shared.
- `{install}/config/ags/skin.css` → `current/skins/ags.css` and
  `{install}/config/ags/skin.json` → `current/skin.json` (bar configDir `ags`).
- Keep the palette-independent cache key
  `sha256(templates_hash ‖ manifest_hash)`, `cache/skins/<hash>/meta.json`,
  optional `desired.json` `skin` (default `glass`), current/history
  projections, appearance hash, refresh/restart lanes, fail-closed semantics,
  inspect/doctor/prune.
- No runtime special-case for the assets repo checkout path; discovery stays
  on the installed spine.

## Acceptance

- [ ] `SkinExpander` parses the canonical grouped TOML/JSON manifest, validates
      `schema_major == 1`, `name`, the six groups and their token sets, and
      rejects unknown groups/tokens/structure/variant keys.
- [ ] Dimensions accept a number or a `<number>px` string; `skin.json` stores
      every numeric token as a raw number (never a unit string).
- [ ] Template discovery is `*.gtk.css.j2` → `<configDir>.css`; unknown and
      unresolved `{{...}}` placeholders fail closed with the template path.
- [ ] CSS substitutes numeric tokens as `<number>px`, `cell-pad` and string
      tokens verbatim, including layout tokens.
- [ ] `skin.json` is the grouped `version: 1` map with numeric values; no flat
      `[tokens]`/`[apps]` shape remains.
- [ ] Manifest discovery includes `skins/manifests/<name>.toml|json` on the
      installed spine.
- [ ] A test expands a fixture copied from the real `glass` manifest plus an
      `ags.gtk.css.j2` template and asserts `border-radius: 12px`,
      `border: 1.5px solid @primary`, `padding: 8px 9px`, grouped numeric
      `12`/`1.5`/`312`, and the `config/ags/skin.css` pointer.
- [ ] The old 18 wrong-format tests are rewritten; cache/fallback/pointer,
      inspect/doctor/prune and appearance tests stay green.
- [ ] Expander/port docstrings, README, ARCHITECTURE and CHANGELOG describe the
      canonical interface.
- [ ] `make gates` and `make gates-ci` pass.
- [ ] Release metadata is the next annotated minor (`v0.8.0`); no existing tag
      is moved.

## Evidence

Baseline repro (main `236e81e`, real assets):

```sh
uv run python - <<'PY'
from pathlib import Path
from runtime.adapters.hashing import canonical_hash_dir, hash_file, skin_entry_hash
from runtime.adapters.skin_expander import SkinExpander
m = Path('/home/inumaki/Development/dotfiles/dotfiles-assets/skins/manifests/glass.toml')
t = Path('/home/inumaki/Development/dotfiles/dotfiles-assets/skins/templates')
h = skin_entry_hash(canonical_hash_dir(t), hash_file(m))
SkinExpander().expand('glass', m, t, Path('/tmp/opencode/skin-baseline')/h)
PY
# ValueError: skin manifest has unknown keys
# ['ink', 'layout', 'rhythm', 'schema_major', 'shape', 'surface', 'type']
```

Real manifest shape: `dotfiles-assets/skins/manifests/glass.toml:11-48`
(`schema_major`, `name`, six groups; dimensions currently spelled `"12px"`).
Consumer contract: `ags-platform/ports/skin.ts` `parseSkinJson` (grouped map,
`version: "1"`, raw numbers for numeric tokens).

## Implementation map

- `src/runtime/adapters/skin_expander.py` — canonical parse/validate,
  `*.gtk.css.j2` discovery, unit rendering, grouped `skin.json`.
- `src/runtime/ports/skin_expander.py` — port docstring.
- `src/runtime/application/derive.py` — manifest discovery under
  `skins/manifests/`.
- `tests/fixtures/skins/` — real-shape manifest + `ags.gtk.css.j2` fixture.
- `tests/unit/test_skin_expander.py`, `tests/unit/test_skins.py` — rewrite.
- `README.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md`.
