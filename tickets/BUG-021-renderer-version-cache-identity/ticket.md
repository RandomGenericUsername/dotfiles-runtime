---
id: BUG-021
title: "Renderer version is absent from skin/appearance/generation identity, so a conf renderer fix keeps the old cached kitty.conf"
kind: bug
status: in-progress
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-021/
branch: ticket/BUG-021-renderer-version-cache-identity
repro: "After v1.5.1, apply against a live state whose skin/appearance cache was built by v1.5.0 without changing the manifest/template: `dotfiles-runtime converge` (or reconcile) then read `$XDG_STATE_HOME/dotfiles/current/appearance/kitty.conf`; it still contains `font_size 12px` / `window_padding_width 9px` because `skin_entry_hash` and the appearance/generation identities do not include the renderer version."
observed: "The v1.5.1 conf renderer is correct in isolation (bare points), but the live appearance cache entry and committed generation are reused: `skin_entry_hash(templates_hash, manifest_hash)` is unchanged, so `appearance_generation_hash(palette, skin, mode)` and the generation content hash are unchanged. The old `appearance/kitty.conf` bytes (built by v1.5.0) stay current and kitty `load_config` still reports BadLine for `12px`/`9px`."
expected: "A renderer/artifact-format version participates in skin entry identity and therefore in appearance and generation identity. Bumping it for the v1.5.1 conf fix makes the next converge build a fresh skin/appearance/generation with `font_size 9`, `window_padding_width 6.75`, bare `background_opacity`, and no px/pt; CSS output for unchanged inputs stays byte-identical. Repeating apply at the same renderer version is a no-op; only a version bump regenerates."
---

# Renderer version is absent from skin/appearance/generation identity

## Triage

- `kind: bug` — cache/generation identity covers authored inputs only
  (`templates_hash`, `manifest_hash`), not runtime renderer behavior. v1.5.1
  changed `conf` rendering without changing either input, so the old entry is
  a legal cache hit.
- `lane: 2` — bounded hashing/identity change plus regression tests; no
  OpenSpec change.
- `severity: high` — the shipped kitty fix cannot reach a live generation on
  upgrade; kitty keeps rejecting the stale `px` lines.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` returned 0 on main
  (`a68441f`) before `create BUG-021 renderer-version-cache-identity`.

## Evidence

- `Evidence:` `src/runtime/adapters/hashing.py:409-419` defines skin identity
  as `sha256(templates_hash ‖ manifest_hash)`; no renderer/format version is
  an input.
- `Evidence:` `src/runtime/application/derive.py:644-647` returns
  `self._seeder.load_skin_entry(target)` whenever the hash-named cache dir
  exists, so unchanged inputs are a hit after a renderer-only change.
- `Evidence:` `src/runtime/adapters/appearance.py:731-742` reuses the
  appearance cache entry keyed by `appearance_generation_hash(palette,
  skin_entry.entry_hash, mode)`; with an unchanged skin hash the old composed
  `kitty.conf` is returned unchanged.
- `Evidence:` `src/runtime/adapters/generation_store.py:188-204` includes
  `skin.entryHash` and `appearance.artifactHash` in the generation identity, so
  the old generation remains the content-current generation.
- `Evidence:` the live artifact read for BUG-020 still contains
  `font_size 12px` / `window_padding_width 9px`; kitty 0.50.0 reports
  `BadLine(ValueError(... '12px'))` and `BadLine(ValueError(... '9px'))`
  (BUG-020 outcome, 2026-11-11).

## Repro

```bash
# Same authored manifest/template, renderer changed between v1.5.0 and v1.5.1:
uv run python - <<'PY'
from pathlib import Path
from runtime.adapters.hashing import canonical_hash_dir, hash_file, skin_entry_hash
from runtime.adapters.skin_expander import (
    expand_v2_template, load_skin_manifest, resolve_v2_token_document,
)
templates = Path("tests/fixtures/skins-v2/templates")
manifest = Path("tests/fixtures/skins-v2/manifests/terminal-host.toml")
skin = skin_entry_hash(canonical_hash_dir(templates), hash_file(manifest))
tokens = resolve_v2_token_document(load_skin_manifest(manifest), "dark")
conf = expand_v2_template(
    "font_size {{SKIN_TYPE_SIZE_BODY}}\n"
    "window_padding_width {{SKIN_SURFACE_WINDOW_PADDING}}\n",
    tokens,
    format="conf",
)
print("skin_entry_hash:", skin)
print(conf, end="")
PY
```

## Observed

```text
skin_entry_hash: <same as v1.5.0 for identical inputs>
font_size 9
window_padding_width 6.75
```

The renderer now emits bare points, but the identity printed above is byte-for-
byte the pre-fix identity, so an existing cache entry/generation is reused and
the live `appearance/kitty.conf` keeps the `12px`/`9px` bytes.

## Expected

- `skin_entry_hash` includes a renderer/artifact-format version constant.
- Bumping that constant changes skin entry hash, appearance hash and generation
  hash for otherwise unchanged inputs, forcing a fresh render.
- Fresh generation contains `font_size 9`, `window_padding_width 6.75` and a
  bare `background_opacity` for terminal-host/terminal; no `px` or `pt`.
- CSS artifacts for unchanged inputs are byte-identical to the pre-bump output.
- Same renderer version across repeated applies is a no-op (idempotent).

## Fix notes

Add one explicit renderer/artifact-format version to the skin entry hash
(`skin_entry_hash`) so it propagates through `skinHash` →
`appearance_generation_hash` → generation content hash. Keep the rendering
path untouched: the version only invalidates identity/cache. Add regression
tests for version participation, old-entry non-reuse, identical CSS bytes,
bare kitty points and `kitty +runpy load_config` zero bad lines. No assets,
composition, provisioning, contracts, apps, live-state or apply change.
