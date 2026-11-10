---
id: REQ-024
title: "Format-aware runtime skin consumers (Phase 3A-3: F-3.1–F-3.5)"
kind: requirement
status: done
lane: 2
links:
  design: dotfiles-v3/docs/110-format-aware-skin-consumers.md
  handoff: contracts/tickets/REQ-018-format-aware-consumers/ticket.md
  openspec: none
  bmad: none
worktree: .worktrees/REQ-024/
branch: ticket/REQ-024-format-aware-runtime-artifacts
---

# Format-aware runtime skin consumers (Phase 3A-3)

## Requirement

Implement the runtime Phase 3A-3 slice (`F-3.1`–`F-3.5`) against the frozen
contracts `v0.10.0` handoff. A skin may emit an honest non-CSS appearance
artifact next to its CSS targets, without widening `appearance.targets` and
without changing any existing CSS bytes.

Scope:

1. **Contract pin.** Pin contracts `v0.10.0` by its annotated tag object and
   vendor/embed the updated `generation-manifest.schema.json`. The embedded
   copy, the conformance copy and the pin gate move in one commit.
2. **Fail-closed template discovery.** Map a template suffix to
   `(family, format, output)`:
   - `<consumer>.gtk.css.j2` → `targets`, `css`, `<consumer>.css`
   - `<consumer>.rasi.j2` → `artifacts`, `rasi`, `<consumer>.rasi`
   - `<consumer>.conf.j2` → `artifacts`, `conf`, `<consumer>.conf`
   - `<consumer>.theme.j2` → `artifacts`, `theme`, `<consumer>.theme`
   Any other `*.j2` file is an explicit error, never silently ignored.
3. **Format-aware renderer.** CSS keeps the existing pipeline and is
   byte-identical to `v1.2.0`. Non-CSS artifacts use the same resolved token
   context with format rules: rasi role references are hyphenated, no
   `alpha()`, no literal hex; conf/theme are reserved paint-free shapes.
   Layout placeholders, conditionals, unknown placeholders and unresolved
   placeholders stay fail-closed.
4. **Publication.** The appearance cache writes `appearance/<consumer>.<ext>`
   for every artifact. The generation manifest publishes
   `appearance.artifacts` as `{consumer, path, format}` when present, while
   `appearance.targets` stays CSS-only and unchanged. Consumer/path uniqueness
   and `path == appearance/<consumer>.<format>` are enforced at runtime.
5. **GenerationStore.** Artifacts are copied into the generation, included in
   `verify()`, validated against contracts `v0.10.0`, and covered by the
   existing composed-appearance GC protection and content identity.
6. **Tests/fixtures.** CSS-only generations stay byte-identical; valid rasi,
   conf and theme shapes; unknown suffix; duplicate consumer/path; extension
   mismatch; missing artifact; current artifact GC protection; no literal
   hex/structure in non-CSS output; AGS CSS consumers unaffected.
7. **Release.** `v1.3.0` annotated tag after green gates; GitHub Release with
   only the wheel and sdist. No contracts/assets/provisioning/composition/apps
   edit and no apply.

## Acceptance

- [x] `CONTRACT_PIN` is the contracts `v0.10.0` tag object; embedded,
      conformance and pinned `generation-manifest` schemas agree.
- [x] Template discovery maps the four known suffixes and fails closed on an
      unknown `*.j2` suffix with an explicit error.
- [x] Existing CSS-only skins render byte-identical CSS and publish no
      `appearance.artifacts` key.
- [x] A valid rasi template renders `appearance/<consumer>.rasi`, aliases role
      references with hyphens, and contains no `alpha()`, literal hex,
      layout or structure.
- [x] `conf` and `theme` are accepted reserved shapes with their canonical
      extensions; CSS is rejected in `artifacts`.
- [x] The generation manifest publishes artifacts without changing
      `appearance.targets`; duplicate consumer/path and extension/format
      mismatches fail closed.
- [x] `GenerationStore` copies every artifact, `verify()` requires it, and GC
      protects the current generation's composed appearance cache.
- [x] `make gates`, `make gates-ci`, `make ci-gates-parity` and targeted
      appearance/generation/GC/skin suites pass.
- [x] `v1.3.0` is an annotated tag on the merged release commit; `main` and
      the tag are pushed; the GitHub Release carries only the wheel and sdist.

## Evidence

- Design: `dotfiles-v3/docs/110-format-aware-skin-consumers.md` §3.1–§3.4,
  §8 Phase 3A-3 (`F-3.1`–`F-3.5`).
- Contract handoff: `contracts/tickets/REQ-018-format-aware-consumers/ticket.md`
  (“Downstream handoff”); contracts tag `v0.10.0` (tag object
  `5c7e133eb86dcd8883626db418dca4c8933da653`, peeled
  `8af8aca54e7107617c4d31655a6eed2665f9b751`).
- Current discovery/renderer: `src/runtime/adapters/skin_expander.py`
  (`_discover_templates`, `render_v2_templates`, `_v2_placeholder_values`).
- Current publication/validation: `src/runtime/adapters/appearance.py`
  (`AppearanceGenerator.ensure`) and
  `src/runtime/adapters/generation_store.py` (`_populate`,
  `_validate_manifest`, `verify`, `_manifest_layer_refs`).
- Current target derivation:
  `src/runtime/domain/models.py::skin_config_dirs` (CSS-only by design).

## Verification

Commands run from `.worktrees/REQ-024/`, branch
`ticket/REQ-024-format-aware-runtime-artifacts`, base `v1.2.0` (`86a2894`).

| Claim | Command | Result |
| --- | --- | --- |
| Full gate set | `make gates` | PASS — `2529 passed, 3 skipped, 11 deselected`; lint/contracts-check/hermetic/lock-check/docs-check clean |
| CI gate set | `make gates-ci` | PASS — same six gates, rc 0 |
| Workflow parity | `make ci-gates-parity` | `PASS gates=[...] jobs=[...] exempt=['ci-gates-parity', 'release']` |
| Cross-repo pin | `CONTRACTS_REPO=../contracts make contract-pin-check` | `PASS — vendored bindings match contracts/generated at pin 5c7e133...` |
| Targeted artifacts/skins/generations/GC | `uv run pytest -q tests/unit/test_format_aware_artifacts.py tests/unit/test_skin_expander.py tests/unit/test_skins.py tests/unit/test_appearance.py tests/unit/test_appearance_v2_assets.py tests/unit/test_generations_v3.py tests/unit/test_generation_gc.py tests/unit/test_wlogout_skin_consumer.py tests/unit/test_contract_schema_conformance.py tests/unit/test_contract_pin_check.py` | `192 passed, 1 skipped` |

### Suffix → family/format/output mapping

| Template name | Family | Format | Output |
| --- | --- | --- | --- |
| `<consumer>.gtk.css.j2` | `targets` | `css` | `<consumer>.css` |
| `<consumer>.rasi.j2` | `artifacts` | `rasi` | `<consumer>.rasi` |
| `<consumer>.conf.j2` | `artifacts` | `conf` | `<consumer>.conf` |
| `<consumer>.theme.j2` | `artifacts` | `theme` | `<consumer>.theme` |

Any other `*.j2` file raises `ValueError: skin template '<name>' has an unknown
suffix`; a CSS/artifact consumer collision raises
`skin templates collide on consumer '<consumer>'`.

### Artifact manifest example

```json
"appearance": {
  "tokensPath": "appearance/tokens.json",
  "targets": [
    { "configDir": "ags", "path": "appearance/ags.css" }
  ],
  "artifacts": [
    { "consumer": "rofi", "path": "appearance/rofi.rasi", "format": "rasi" }
  ]
}
```

The CSS-only case omits `artifacts` entirely and keeps the `v1.2.0` manifest
shape byte-for-byte.

### Commits

| Commit | Subject |
| --- | --- |
| `b9682d1` | `docs(REQ-024): plan format-aware runtime artifacts` |
| `0dfbf96` | `chore(REQ-024): start — record worktree/branch, move to in-progress` |
| `db5f4a5` | `feat(REQ-024): render format-aware appearance artifacts` |
| release commit (the `v1.3.0` tag target) | `chore(REQ-024): release v1.3.0` |

### Status

**Done on `ticket/REQ-024-format-aware-runtime-artifacts`.** Runtime discovery,
format-aware rendering, appearance publication and generation-store
copy/verify/GC are implemented against contracts `v0.10.0`. Merge to `main`
fast-forward, create the annotated `v1.3.0` tag on the release commit, push
`main` + the tag, and publish the GitHub Release with only the wheel and
sdist. No assets/provisioning/composition/app edit and no apply was performed.

### Downstream pins needed

- `dotfiles-assets` Phase 3A-2: ship `gtk3.gtk.css.j2`,
  `gtk4.gtk.css.j2`, `rofi.rasi.j2` and re-pin runtime `v1.3.0` (the runtime
  already fails closed on any template it cannot classify).
- `dotfiles-provision` Phase 3A-4: add the `gtk3`/`gtk4`/`rofi` stable
  `appearance` links and last-position imports.
- Composition Phase 3A-5: pin contracts `v0.10.0` and runtime `v1.3.0` in the
  documented order; no downstream pin is changed by this slice.

## Non-goals

- No assets templates, provisioning links, composition pins or live apply.
- No per-consumer adapter classes; one table-driven dispatcher.
- No `appearance.targets` widening and no generation manifest version bump.
- No kitty/btop colour projection (palette-only remains palette-only).
