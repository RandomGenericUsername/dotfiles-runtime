---
id: REQ-016
title: "Consume the real v2 appearance manifests (TOML, nested groups, modes)"
kind: requirement
status: in-progress
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-016/
branch: ticket/REQ-016-appearance-v2-manifests
---

# REQ-016 — Consume the real v2 appearance manifests (TOML, nested groups, modes)

## Requirement

After v0.9.0 the runtime still only parses frozen v1 skin manifests
(`schema_major = 1`, flat `[shape]`/`[surface]`/... groups) plus a v1
`skin.json` translation. `dotfiles-assets v0.12.0` ships the frozen
`contracts v0.7.0` v2 appearance manifests, so the real assets are rejected:

```python
SkinExpander().expand("glass", dotfiles-assets/skins/manifests/glass.toml, ...)
# ValueError: skin manifest has unknown keys ['groups', 'schemaVersion']
```

The runtime must consume the real v2 TOML manifests while keeping the v1
(schema_major=1) path working until the tuple bump.

1. **Parse v2 manifests.** Accept `schemaVersion = 2`, `name`, nested
   `[groups.surface.{panel,card,cell,control,pill}]`, `[groups.state.*]`,
   `[groups.type]`, `[groups.ink]`, `[groups.layout.<app>]` and optional
   `[mode.dark]`/`[mode.light]` complete same-shape overrides. Validate the
   document against the vendored `appearance-manifest` schema; reject unknown
   structure/variant/hex/transition values. Keep `schema_major = 1` manifests
   working unchanged.
2. **Resolve mode before writing tokens/CSS.** Read the palette mode when
   `colors.yaml` exposes `mode`/`scheme`/`appearance`/`variant` or
   `is_dark`/`dark`; otherwise default to `dark` and document that the
   producer must eventually emit a mode. Apply the selected override by
   replacing each provided group as a whole before writing tokens or
   expanding templates. Mode remains part of
   `appearanceHash = sha256("appearance.v2\n" + paletteHash + "\n" +
   skinHash + "\n" + mode)`.
3. **tokens.json is the v2 resolved document.** It must be
   `{version: 2, surface, state, type, ink, layout}` (top-level groups, no
   `groups` wrapper), mode-resolved and validated against the vendored
   `appearance-json` schema.
4. **Expand v2 flattened placeholders.** A placeholder is the full nested
   token path uppercased with every non-alphanumeric run replaced by `_`
   (`surface.panel.bg` → `{{SKIN_SURFACE_PANEL_BG}}`, `surface.cell.hover-bg`
   → `{{SKIN_SURFACE_CELL_HOVER_BG}}`). Lengths render `<n>px`;
   `letter-spacing` renders `<n>em`; `font-weight` stays unitless. There are
   no conditionals; an unknown, unresolved or layout placeholder fails closed.
5. **Compose the same way.** Each generation `<configDir>.css` remains the
   legacy palette CSS + rendered palette role block + mode-resolved expanded
   skin CSS. `current/appearance` stays the single-hop pointer, with stable
   convenience links and the v1 pointers retained during migration.
6. **Tests** cover the real v2 `glass`/`flat`/`paper` manifests × both modes,
   `tokens.json` validation, flattened placeholders/units, override
   completeness, the v1 translation path, cache hashes, the pointer and the
   composed CSS palette/role blocks.

## Acceptance

- [ ] The real `dotfiles-assets` v2 `glass.toml` expands through
      `SkinExpander().expand(..., <v2 templates>)` with no "unknown keys".
- [ ] v2 manifests validate against the vendored `appearance-manifest` schema;
      structure/variant/hex/transition documents fail closed.
- [ ] The v1 `schema_major = 1` manifest + `skin.json` translation path still
      passes its existing tests.
- [ ] `tokens.json` is the mode-resolved top-level v2 document and validates
      against the vendored `appearance-json` schema.
- [ ] `mode.light`/`mode.dark` override groups are applied completely before
      tokens/CSS; omitted groups inherit the base groups.
- [ ] Palette mode detection covers the documented keys; the absent-mode gap
      defaults to `dark` and is documented for the producer.
- [ ] v2 placeholder flattening and the px/em/unitless rules are proven on the
      real `glass`/`flat`/`paper` templates in both modes.
- [ ] Composed CSS keeps the legacy palette CSS + role block + skin CSS order,
      and the single-hop appearance pointer and v1 pointers are unchanged.
- [ ] Cache hashes and idempotency remain `sha256(templates_hash ‖
      manifest_hash)` for a skin and `f(palette, skin, mode)` for appearance.
- [ ] Release metadata is the next patch (`v0.9.1`); no existing tag moves.
- [ ] `make gates` and `make gates-ci` pass.

## Evidence

Baseline reproduction on `main` (v0.9.0), 2026-11-06:

```sh
uv run python - <<'PY'
from pathlib import Path
from runtime.adapters.skin_expander import SkinExpander
assets = Path("../dotfiles-assets")
SkinExpander().expand(
    "glass",
    assets / "skins/manifests/glass.toml",
    assets / "skins/templates",
    Path("/tmp/opencode/out"),
)
PY
# ValueError: skin manifest has unknown keys ['groups', 'schemaVersion']:
#   ../dotfiles-assets/skins/manifests/glass.toml
```

Final evidence (real v2 assets expansion) is recorded when the acceptance
boxes are closed.
