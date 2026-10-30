---
id: REQ-006
title: "Palette vocabulary contract (palette.v1): declared roles, conformance validation, probe tests"
kind: requirement
status: in-progress
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-006/
branch: ticket/REQ-006-palette-vocabulary-contract
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

- [x] `runtime.domain.palette_vocabulary` declares v1 (50 roles +
      `on_backdrop`) with a source comment and version.
- [x] Palette cache build fails closed with a typed error naming missing roles;
      pre-swap ordering preserved.
- [x] `colors.gtk.css` role definitions are validated too.
- [x] `inspect palette` / doctor expose vocabulary conformance.
- [x] Probe test fails when a role is removed/renamed; passes on the tpr
      `v0.5.0` fixture; extras are accepted.
- [ ] Integration fixture: a real tpr `v0.5.0` palette passes; a v0.4.0
      palette fails naming `on_backdrop`. (blocked: tpr v0.5.0 REQ-022 not
      published yet; unit fixtures cover both shapes)
- [ ] `make gates` green; release `v0.5.0` after tpr `v0.5.0` is published.
      (gates green at implementation commit; release intentionally pending)

## Evidence / Verify

- Evidence: fail-closed swap ordering in
  `src/runtime/application/apply_wallpaper.py`; artifact list in
  `src/runtime/adapters/consumer_path_spec.py`; cache entry build in
  `src/runtime/application/`.
- Verify: `make gates`; `uv run dotfiles-runtime inspect palette` after a
  tpr v0.5.0 set; fixture test for the v0.4.0 failure.

## Progress / evidence (2026-10-30)

Implementation commits on `ticket/REQ-006-palette-vocabulary-contract`
(worktree `.worktrees/REQ-006/`):

- `8cb1489` — vocabulary, conformance, gate wiring, inspect/doctor, fixtures.
- `57cadbe` — reconcile cache-hit validation before repoint.

Delivered:

- `src/runtime/domain/palette_vocabulary.py` — `PALETTE_VOCABULARY_VERSION =
  "1"`, ordered `PALETTE_VOCABULARY_V1` (50 `MaterialRole` values in tpr
  declaration order + `on_backdrop`), frozenset projection, source comment
  naming theme-producer `v0.5.0` REQ-022, and typed
  `PaletteVocabularyError`.
- `src/runtime/application/palette_conformance.py` — no-YAML subset parsers
  for `colors.yaml` (`key: "#hex"`) and `colors.gtk.css`
  (`@define-color role #hex;`); missing/malformed/extra reporting;
  `require()` raises the typed error naming roles. Extras are allowed and
  reported only.
- `DerivationPipeline.ensure_palette` — for a **tpr** producer, validates the
  staged artifacts BEFORE `drain_work_dir`/staging rename and also validates
  an existing cache hit. Failure therefore aborts before the BUG-001 visible
  swap and leaves no cache entry or staging dir.
- `ReconcileDesktopStateUseCase._ensure_entries` — validates a complete
  existing tpr palette before the symlink repoint; a miss/incomplete entry
  still regenerates through the pipeline (which validates the build).
- `inspect palette` — reports version, per-artifact conformance, extras; exits
  1 for a nonconforming semantic palette. `doctor` — adds a `vocabulary`
  verdict (diverged when roles are missing/malformed, repair quarantines and
  regenerates). Legacy csg palettes report `n/a`/`ok` because csg is not a
  `palette.v1` producer.
- Probe fixture `tests/fixtures/palette-vocabulary-v1.json` pins the 51-role
  declaration. Palette fixtures: `v1-valid`, `v1-missing-role`,
  `v1-extra-roles`, `v0.4.0`; the v0.4.0 shape fails naming `on_backdrop`.

Deviation / risk:

- The build-time gate is scoped to `producer_name == "tpr"`. CSG emits only
  `background`/`foreground`/ANSI colors, cannot satisfy `palette.v1`, and is
  still the documented default until REQ-007 retires it. A blanket gate would
  break the default producer; the ticket's vocabulary is explicitly the tpr
  role set. `inspect`/`doctor` never dirty a healthy legacy csg desktop.
- tpr `v0.5.0` (REQ-022) is not yet published, so no live tpr run was
  attempted. The `v0.4.0` fixture is the current tpr projection and fails
  naming `on_backdrop` — expected pre-pin behavior by design.

Evidence (executed):

- `uv run pytest tests/unit/test_palette_vocabulary.py
  tests/unit/test_cli_inspect_palette.py -q` → `23 passed` (exit 0).
- `uv run pytest tests/unit/test_palette_vocabulary.py
  tests/unit/test_cli_inspect_palette.py tests/unit/test_reconcile.py -q` →
  `64 passed` (exit 0).
- `make gates` (worktree root) → `EXIT:0`:
  - `uv run pytest -q -m "not integration"` → `2324 passed, 3 skipped, 22
    deselected`.
  - `uv run ruff check src` → `All checks passed!`.
  - contracts-check → `48 passed, 1 skipped`.
  - hermetic → same fast suite `2324 passed, 3 skipped, 22 deselected`.
  - `uv lock --check` → `Resolved 25 packages in 2ms`; docs-check silent
    success.
- Verify probe: `uv run pytest tests/unit/test_palette_vocabulary.py::test_declared_vocabulary_matches_probe_fixture -q` → `1 passed`.
