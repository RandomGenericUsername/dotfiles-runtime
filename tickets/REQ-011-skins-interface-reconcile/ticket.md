---
id: REQ-011
title: "Reconcile the skin expander to the canonical skin interface"
kind: requirement
status: done
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

Dimensions are unitless numbers in both TOML and JSON. The assets repo was
canonicalized during this work (`5a62c2d` unitless tokens, `db33e7e`
`<configDir>.gtk.css.j2` names), so the runtime enforces the canonical form and
rejects a unit string such as `"12px"`; CSS renders `<number>px`.

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

- [x] `SkinExpander` parses the canonical grouped TOML/JSON manifest, validates
      `schema_major == 1`, `name`, the six groups and their token sets, and
      rejects unknown groups/tokens/structure/variant keys.
- [x] Dimensions are unitless numbers; a unit string fails closed. `skin.json`
      stores every numeric token as a raw number (never a unit string).
- [x] Template discovery is `*.gtk.css.j2` → `<configDir>.css`; unknown and
      unresolved `{{...}}` placeholders fail closed with the template path.
- [x] CSS substitutes numeric tokens as `<number>px`, `cell-pad` and string
      tokens verbatim, including layout tokens.
- [x] `skin.json` is the grouped `version: 1` map with numeric values; no flat
      `[tokens]`/`[apps]` shape remains.
- [x] Manifest discovery includes `skins/manifests/<name>.toml|json` on the
      installed spine.
- [x] A test expands a fixture copied from the real `glass` manifest plus an
      `ags.gtk.css.j2` template and asserts `border-radius: 12px`,
      `border: 1.5px solid @primary`, `padding: 8px 9px`, grouped numeric
      `12`/`1.5`/`312`, and the `config/ags/skin.css` pointer.
- [x] The old 18 wrong-format tests are rewritten; cache/fallback/pointer,
      inspect/doctor/prune and appearance tests stay green.
- [x] Expander/port docstrings, README, ARCHITECTURE and CHANGELOG describe the
      canonical interface.
- [x] `make gates` and `make gates-ci` pass.
- [x] Release metadata is the next annotated minor (`v0.8.0`); no existing tag
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
(`schema_major`, `name`, six groups; at the baseline commit the dimensions were
spelled `"12px"`, and the assets repo canonicalized them to unitless numbers in
`5a62c2d` during this work). Consumer contract: `ags-platform/ports/skin.ts`
`parseSkinJson` (grouped map, `version: "1"`, raw numbers for numeric tokens).

## Implementation evidence

Executed on the ticket worktree (2026-11-05):

```sh
uv run pytest -q tests/unit/test_skin_expander.py tests/unit/test_skins.py
# 29 passed
```

The fixture `tests/fixtures/skins/manifests/glass.toml` is byte-identical to
the real assets manifest (`cmp` exit 0); `tests/fixtures/skins/templates/ags.gtk.css.j2`
is the canonical template name.

```sh
uv run python - <<'PY'
from pathlib import Path
from runtime.adapters.hashing import canonical_hash_dir, hash_file, skin_entry_hash
from runtime.adapters.skin_expander import SkinExpander
m = Path('/home/inumaki/Development/dotfiles/dotfiles-assets/skins/manifests/glass.toml')
t = Path('/home/inumaki/Development/dotfiles/dotfiles-assets/skins/templates')
h = skin_entry_hash(canonical_hash_dir(t), hash_file(m))
entry = SkinExpander().expand('glass', m, t, Path('/tmp/opencode/skin-canonical')/h)
print(sorted(entry.artifact_hashes))
PY
# ['ags.css', 'skin.json']
```

Rendered real assets CSS contains `border-radius: 12px`, `border: 1.5px solid
@primary`, `padding: 8px 9px` and `min-width: 312px`; the real `skin.json`
groups are `version,shape,surface,ink,type,rhythm,layout` with numeric
`radius-panel=12`, `border-w=1.5`, `panel-width=312`.

```sh
make gates
# test       2239 passed, 3 skipped, 10 deselected
# lint       All checks passed
# contracts  48 passed, 1 skipped
# hermetic   2239 passed, 3 skipped, 10 deselected
# lock-check Resolved 25 packages (no changes)
# docs-check exit 0
```

```sh
make gates-ci
# same set and results as make gates; exit 0
```

## Interface ambiguity resolved

1. **Dimension spelling.** At ticket start the canonical statement said
   unitless numbers while the assets manifests spelled dimensions `"12px"`.
   The assets repo canonicalized them during the work (`5a62c2d`), so the
   conflict is gone. Resolution: enforce strict unitless numbers; the fixture
   is a byte-identical copy of the canonical real `glass.toml` and the test
   asserts numeric `12`/`1.5`/`312`.
2. **Template/configDir name.** At ticket start the real assets file was
   `templates/ags-bar.gtk.css.j2`, but the canonical bar configDir is `ags`
   (`dotfiles-v3/dotfiles/provisioning/apps.yaml`). The assets repo renamed it
   to `ags.gtk.css.j2` during the work (`db33e7e`). Resolution: the runtime
   implements only the canonical `<configDir>.gtk.css.j2` → `<configDir>.css`
   rule (no runtime special-case); the real assets now produce `ags.css`.
3. **Group order.** The real TOML lists `shape,rhythm,surface,ink,type,layout`;
   `parseSkinJson`'s declared group order is
   `shape,surface,ink,type,rhythm,layout`. Resolution: `skin.json` emits the
   contract order; TOML table order is irrelevant.
4. **`ink-over`.** The canonical string-token list omits it, but it is a real
   frozen v1 token (`"auto"`). It is treated as a string token; no new field was
   invented.

## Implementation map

- `src/runtime/adapters/skin_expander.py` — canonical parse/validate,
  `*.gtk.css.j2` discovery, unit rendering, grouped `skin.json`.
- `src/runtime/ports/skin_expander.py` — port docstring.
- `src/runtime/application/derive.py` — manifest discovery under
  `skins/manifests/`.
- `tests/fixtures/skins/` — real-shape manifest + `ags.gtk.css.j2` fixture.
- `tests/unit/test_skin_expander.py`, `tests/unit/test_skins.py` — rewrite.
- `README.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md`.
