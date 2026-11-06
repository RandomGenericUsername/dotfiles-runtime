---
id: REQ-012
title: "Accept the skins v0.6.0 shape.hairline-w token"
kind: requirement
status: in-progress
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-012/
branch: ticket/REQ-012-skins-hairline-token
---

# REQ-012 — Accept the skins v0.6.0 shape.hairline-w token

## Requirement

The integration gate expanded the real assets manifests (contracts v0.6.0) and
the runtime rejected them:

```text
ValueError: skin manifest group 'shape' has unknown token(s) ['hairline-w']
```

`src/runtime/adapters/skin_expander.py` hardcodes `_TOKEN_GROUPS` and
`_NUMERIC_TOKENS` from the v0.5.2 vocabulary (23 tokens). contracts v0.6.0
added `shape.hairline-w` (unitless number) and froze the layout tokens as
TSX-owned; runtime behaviour is otherwise unchanged.

Required correction:

- Add `hairline-w` to the `shape` group tuple and to `_NUMERIC_TOKENS` (24
  tokens total).
- Update the expander module docstring/example to the 24-token vocabulary and
  explain the panel border versus hairline split.
- Replace the copied fixtures with the real 24-token manifests: `glass`
  (`hairline-w = 1`), `flat` (`0`) and `paper` (`1`).
- Assert `hairline-w` is accepted and renders as `1px` in CSS;
  `hairline-w = "1px"` (a unit string) is rejected; and `skin.json` carries
  `shape.hairline-w` as a raw number. Grouping/order stays as declared.

## Acceptance

- [x] `shape` and `_NUMERIC_TOKENS` include `hairline-w`; the vocabulary is 24
      tokens.
- [x] The expander docstring/example lists the 24-token vocabulary and names
      `hairline-w` as the card/row/badge hairline distinct from the panel
      `border-w`.
- [x] Fixtures under `tests/fixtures/skins/manifests/` are the real `glass`,
      `flat` and `paper` manifests with `hairline-w` 1, 0 and 1.
- [x] A fixture expansion renders `hairline-w` as `1px` in CSS and stores
      `shape.hairline-w` as the raw number `1` in `skin.json`.
- [x] `hairline-w = "1px"` fails closed with the unitless-number error.
- [x] `skin.json` group order remains
      `version,shape,surface,ink,type,rhythm,layout`; existing expansion
      assertions stay green.
- [x] Release metadata is the next patch (`v0.8.1`); no existing tag is moved.
- [x] `make gates` and `make gates-ci` pass.

## Evidence

Baseline on the ticket worktree at `907dd54`, expanding the real assets
manifest:

```sh
uv run python - <<'PY'
from pathlib import Path
from runtime.adapters.hashing import canonical_hash_dir, hash_file, skin_entry_hash
from runtime.adapters.skin_expander import SkinExpander
m = Path('/home/inumaki/Development/dotfiles/dotfiles-assets/skins/manifests/glass.toml')
t = Path('/home/inumaki/Development/dotfiles/dotfiles-assets/skins/templates')
h = skin_entry_hash(canonical_hash_dir(t), hash_file(m))
SkinExpander().expand('glass', m, t, Path('/tmp/opencode/skin-hairline-baseline')/h)
PY
# ValueError: skin manifest group 'shape' has unknown token(s) ['hairline-w']
```

The real `glass.toml` carries `hairline-w = 1` in `[shape]`; the real template
uses `{{SKIN_HAIRLINE_W}}` for card/row/badge hairlines. The 23-token runtime
vocabulary came from `src/runtime/adapters/skin_expander.py:_TOKEN_GROUPS`
(no `hairline-w`).

## Implementation evidence

Executed on the ticket worktree (2026-11-05):

```sh
uv run pytest -q tests/unit/test_skin_expander.py tests/unit/test_skins.py
# 34 passed
```

The three fixtures are byte-identical to the real assets manifests
(`cmp` exit 0 for each):

```sh
for s in glass flat paper; do
  cmp tests/fixtures/skins/manifests/$s.toml \
      /home/inumaki/Development/dotfiles/dotfiles-assets/skins/manifests/$s.toml
done
```

Expanding the real assets templates/manifests now succeeds and renders the
hairline:

```text
glass skin.shape.hairline-w = 1 | border: 1px solid alpha(@on_surface, 0.10);
flat  skin.shape.hairline-w = 0 | border: 0px solid alpha(@on_surface, 0.10);
paper skin.shape.hairline-w = 1 | border: 1px solid alpha(@on_surface, 0.10);
```

```sh
make gates
# test       2244 passed, 3 skipped, 10 deselected
# lint       All checks passed
# contracts  48 passed, 1 skipped
# hermetic   2244 passed, 3 skipped, 10 deselected
# lock-check Resolved 25 packages (no changes)
# docs-check exit 0
# make gates exit=0
```

```sh
make gates-ci
# same set and results as make gates; exit 0
```

Release metadata is `0.8.1` in `pyproject.toml`, `uv.lock` and
`runtime.__version__`; the annotated `v0.8.1` tag is created after the merge.
No existing tag was moved.

## Implementation map

- `src/runtime/adapters/skin_expander.py` — 24-token vocabulary, `hairline-w`
  in `shape` and numeric tokens, docstring example/ownership note.
- `tests/fixtures/skins/manifests/{glass,flat,paper}.toml` — real manifests.
- `tests/fixtures/skins/templates/ags.gtk.css.j2` — hairline placeholder rule.
- `tests/unit/test_skin_expander.py`, `tests/unit/test_skins.py` — acceptance,
  rejection and raw-number assertions.
- `README.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md`, `pyproject.toml`,
  `uv.lock`, `src/runtime/__init__.py` — release metadata and docs.
