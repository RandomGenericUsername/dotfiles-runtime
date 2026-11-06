# Design — skins derivation layer

## 1. Data model

`SkinEntry` mirrors `PaletteEntry`:

```text
hash_algorithm="sha256", kind="skin", entry_hash,
name, input_templates_hash, input_manifest_hash,
artifact_hashes, generated_at
```

`DesktopState.skin: SkinEntry | None = None` is the only state addition. `current.json` stores
only `{hash, name, generated_at}` (same projection rule as the other derived layers); the full
input/artifact hashes live in `cache/skins/<hash>/meta.json`.

`desired.json` gains an optional `skin: "<name>"`. Absent → `glass`; invalid type/name fails
loud with the path and field, matching the existing strict reader.

## 2. Inputs and identity

Resolution is spine-first (never a composition checkout), with the existing opt-in
`DOTFILES_DEV_INPUTS_ROOT` development override:

```text
<install>/skins/<name>.{toml,json}
<install>/skins/<name>/{manifest,skin}.{toml,json}
<install>/config/skins/...            (provisioning copy)
<install>/assets/<tag>/skins/...      (pinned assets checkout, if present)
<install>/skin-templates/ or config/skins/templates/
```

Identity is exactly the frozen spec:

```text
templates_hash = canonical_hash_dir(templates_dir)
manifest_hash  = sha256(manifest file bytes)
entry_hash     = sha256(templates_hash ‖ manifest_hash)
```

`wallpaper_hash`, `palette_hash`, state and time are deliberately absent: a wallpaper or palette
change cannot invalidate a skin.

## 3. Manifest v1 (JSON or TOML)

```toml
name = "glass"
version = 1
description = "optional"

[tokens]
surface_alpha = "0.82"
radius = "12px"
border_width = "1px"

[apps]
ags = "ags.css.tpl"
gtk = "gtk.css.tpl"
```

- `name` must equal the selected skin; `version` must be `1`.
- `apps` is non-empty; each key becomes `current/skins/<key>.css`, each value is a
  templates-relative `.tpl`/`.css` file.
- `tokens` values are scalars (string/int/float/bool); keys normalize to
  `{{SKIN_<UPPER_SNAKE>}}`.
- Unknown keys, unsafe paths, duplicate output names, non-scalar tokens and unresolved
  placeholders fail closed.
- `skin.json` is the resolved flat token map (`sort_keys`, stable) for `skinValue` consumers.

## 4. Cache and pointers

`populate_via_staging` writes `cache/skins/<entry_hash>/` with expanded CSS files, `skin.json`
and `meta.json` (`kind: "skin"`). Current pointers are ordinary AD-6 atomic symlinks:

```text
current/skins/<app>.css  → cache/skins/<hash>/<app>.css
current/skin.json        → cache/skins/<hash>/skin.json
```

Consumer pointers are one static-table addition:

```text
{install}/config/ags/skin.css  → current/skins/ags.css
{install}/config/ags/skin.json → current/skin.json
```

The existing skip-on-missing target/parent rules mean a failed skin derivation never creates or
removes app CSS.

## 5. Failure policy and refresh

`ensure_skin` resolution/expansion failures are caught by reconcile/seed:

- the previous `state.skin` is kept only when its cache entry still exists;
- a structured `logger.error` records `name`, `reason`, `manifest`, `templates`;
- `current/skins/*`, `current/skin.json` and the AGS pointers are not touched.

`appearance_hash` adds a `"skins"` field (the skin entry hash). A skin change therefore makes the
existing applied-appearance ledger stale and live apps refresh through
`ags request palette-refresh`; the restart-only bar's lane is untouched.

## 6. Verification plan

```sh
uv run pytest -q tests/unit/test_skin_expander.py tests/unit/test_skins.py
# cache hit/miss, manifest JSON+TOML, invalid fallback, atomic repoint,
# appearance hash, default glass

uv run pytest -q tests/unit/test_{desired_state,json_state_repository,inspect_cache,doctor_check,prune}.py
make gates
```

The hermetic gate re-runs the fast suite with empty `HOME`/`XDG_*`; all skin tests use tmp
spines/state roots and never a real desktop.
