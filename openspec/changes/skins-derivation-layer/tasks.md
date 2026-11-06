# Tasks — skins derivation layer

Each task ends with its observable check. Chunks are small enough for one focused pass.

## 1. Domain / hashing / schemas

- [x] `SkinEntry`, `DEFAULT_SKIN`, `SkinUnavailableError`; `skin_entry_hash`.
  Check: `uv run pytest -q tests/unit/test_hashing.py` (+ new skin hash test).
- [x] Embedded + canonical `current`/`meta`/`history` schemas gain optional skin fields and stay
  byte-identical.
  Check: `make contracts-check`.
- [x] Desired reader accepts optional `skin` (default `glass`), rejects malformed values.
  Check: `uv run pytest -q tests/unit/test_desired_state.py`.

## 2. Adapter

- [x] `SkinExpander` parses JSON/TOML, validates v1, substitutes `{{SKIN_*}}`, writes CSS +
  `skin.json`, returns `SkinEntry`.
  Check: `uv run pytest -q tests/unit/test_skin_expander.py`.
- [x] Seeder writes/loads skin meta; current skin links and AGS consumer pointers.
  Check: `uv run pytest -q tests/unit/test_seeder.py tests/unit/test_consumer_path_spec.py`.
- [x] `skins` cache layer + verify/prune/inspect ordering.
  Check: `uv run pytest -q tests/unit/test_inspect_cache.py tests/unit/test_prune.py`.

## 3. Application / wiring

- [x] `DerivationPipeline.ensure_skin` + spine/dev discovery.
  Check: `uv run pytest -q tests/unit/test_skins.py -k cache`.
- [x] Reconcile/seed derive the selected skin; failure keeps the previous skin and logs.
  Check: `uv run pytest -q tests/unit/test_skins.py -k fallback`.
- [x] Appearance hash includes skin; `appearance_hash_for_state` reads it.
  Check: `uv run pytest -q tests/unit/test_skins.py -k appearance`.
- [x] Inspect prints skin/tokens; doctor covers entry/links/consumer pointers.
  Check: `uv run pytest -q tests/unit/test_skins.py -k inspect`.
- [x] CLI resolves desired skin (default glass) and wires `SkinExpander` everywhere.
  Check: `uv run pytest -q tests/unit/test_cli_reconcile.py tests/unit/test_cli_crash_recovery.py`.

## 4. Docs / release

- [x] ARCHITECTURE/README/CHANGELOG document the layer and pointers.
  Check: `make docs-check`.
- [x] Ticket acceptance ticked + INDEX updated in the same commit.
  Check: `rg "REQ-010" tickets/INDEX.md`.
- [x] Full gates and annotated `v0.7.0`.
  Check: `make gates && make gates-ci`; `git tag -v v0.7.0`.
