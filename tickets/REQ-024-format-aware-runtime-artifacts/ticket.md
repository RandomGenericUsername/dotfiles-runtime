---
id: REQ-024
title: "Format-aware runtime skin consumers (Phase 3A-3: F-3.1–F-3.5)"
kind: requirement
status: in-progress
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

- [ ] `CONTRACT_PIN` is the contracts `v0.10.0` tag object; embedded,
      conformance and pinned `generation-manifest` schemas agree.
- [ ] Template discovery maps the four known suffixes and fails closed on an
      unknown `*.j2` suffix with an explicit error.
- [ ] Existing CSS-only skins render byte-identical CSS and publish no
      `appearance.artifacts` key.
- [ ] A valid rasi template renders `appearance/<consumer>.rasi`, aliases role
      references with hyphens, and contains no `alpha()`, literal hex,
      layout or structure.
- [ ] `conf` and `theme` are accepted reserved shapes with their canonical
      extensions; CSS is rejected in `artifacts`.
- [ ] The generation manifest publishes artifacts without changing
      `appearance.targets`; duplicate consumer/path and extension/format
      mismatches fail closed.
- [ ] `GenerationStore` copies every artifact, `verify()` requires it, and GC
      protects the current generation's composed appearance cache.
- [ ] `make gates`, `make gates-ci`, `make ci-gates-parity` and targeted
      appearance/generation/GC/skin suites pass.
- [ ] `v1.3.0` is an annotated tag on the merged release commit; `main` and
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

## Non-goals

- No assets templates, provisioning links, composition pins or live apply.
- No per-consumer adapter classes; one table-driven dispatcher.
- No `appearance.targets` widening and no generation manifest version bump.
- No kitty/btop colour projection (palette-only remains palette-only).
