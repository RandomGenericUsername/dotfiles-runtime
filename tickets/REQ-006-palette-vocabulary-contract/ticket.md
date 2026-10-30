---
id: REQ-006
title: "Palette vocabulary contract (palette.v1): declared roles, conformance validation, probe tests"
kind: requirement
status: in-progress
lane: 2
links:
  openspec: none
  bmad: none
worktree: none
branch: none
---

# Palette vocabulary contract (`palette.v1`)

## Requirement

The runtime must validate that every produced palette carries the **declared
role vocabulary** before caching and before the visible swap. This is the seam
that makes silent meaning changes impossible.

Vocabulary v1 = the 50 tpr Material roles + `on_backdrop` (theme-producer
REQ-022, tpr `v0.5.0`). The canonical declaration lives in
`runtime.domain.palette_vocabulary` as a versioned constant, with a vendored
fixture for drift detection. Consumer lints (e.g. dotfiles-assets) may reuse
the same list, but the runtime is the gate.

### Behavior

1. On building a palette cache entry, `colors.yaml` must contain every declared
   role as a top-level key with a valid hex value, and `colors.gtk.css` must
   define every declared role as `@define-color <role> <hex>`. Extra keys are
   allowed (additive changes). Missing, renamed, or malformed roles are typed
   failures that name the role(s) and abort before the visible swap, reusing
   BUG-001's fail-closed ordering.
2. `inspect palette` and `doctor` report the vocabulary version, conformance
   result, and any extra roles (info only).
3. Probe tests ("change of meaning"): a test pins the declared role list
   against `tests/fixtures/palette-vocabulary-v1.json` generated from tpr
   `v0.5.0`; deleting or renaming a role fails the suite. Presence of the role,
   not the alias, is the assertion.
4. Do **not** require the legacy `colors:` list as a consumer contract; it
   remains only because ANSI file projections need the format.
5. Dependency order: validation requiring `on_backdrop` lands with/after tpr
   `v0.5.0`. Unit tests use fixtures; a v0.4.0 palette failing with
   "missing on_backdrop" is the expected pre-pin behavior, by design.

## Acceptance

- [ ] `runtime.domain.palette_vocabulary` declares v1 (50 roles +
      `on_backdrop`) with a source comment and version.
- [ ] Palette cache build fails closed with a typed error naming missing roles;
      pre-swap ordering preserved.
- [ ] `colors.gtk.css` role definitions are validated too.
- [ ] `inspect palette` / doctor expose vocabulary conformance.
- [ ] Probe test fails when a role is removed/renamed; passes on the tpr
      `v0.5.0` fixture; extras are accepted.
- [ ] Integration fixture: a real tpr `v0.5.0` palette passes; a v0.4.0
      palette fails naming `on_backdrop`.
- [ ] `make gates` green; release `v0.5.0` after tpr `v0.5.0` is published.

## Evidence / Verify

- Evidence: fail-closed swap ordering in
  `src/runtime/application/apply_wallpaper.py`; artifact list in
  `src/runtime/adapters/consumer_path_spec.py`; cache entry build in
  `src/runtime/application/`.
- Verify: `make gates`; `uv run dotfiles-runtime inspect palette` after a
  tpr v0.5.0 set; fixture test for the v0.4.0 failure.
