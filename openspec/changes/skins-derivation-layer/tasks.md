# Tasks — skins derivation layer

Each task ends with its observable check. Chunks are small enough for one focused pass.

## 1. Domain / hashing / schemas

- [ ] `SkinEntry`, `DEFAULT_SKIN`, `SkinUnavailableError`; `skin_entry_hash`.
  Check: `uv run pytest -q tests/unit/test_hashing.py` (+ new skin hash test).
- [ ] Embedded + canonical `current`/`meta`/`history` schemas gain optional skin fields and stay
  byte-identical.
  Check: `make contracts-check`.
- [ ] Desired reader accepts optional `skin` (default `glass`), rejects malformed values.
  Check: `uv run pytest -q tests/unit/test_desired_state.py`.

## 2. Adapter

- [ ] `SkinExpander` parses JSON/TOML, validates v1, substitutes `{{SKIN_*}}`, writes CSS +
  `skin.json`, returns `SkinEntry`.
  Check: `uv run pytest -q tests/unit/test_skin_expander.py`.
- [ ] Seeder writes/loads skin meta; current skin links and AGS consumer pointers.
  Check: `uv run pytest -q tests/unit/test_seeder.py tests/unit/test_consumer_path_spec.py`.
- [ ] `skins` cache layer + verify/prune/inspect ordering.
  Check: `uv run pytest -q tests/unit/test_inspect_cache.py tests/unit/test_prune.py`.

## 3. Application / wiring

- [ ] `DerivationPipeline.ensure_skin` + spine/dev discovery.
  Check: `uv run pytest -q tests/unit/test_skins.py -k cache`.
- [ ] Reconcile/seed derive the selected skin; failure keeps the previous skin and logs.
  Check: `uv run pytest -q tests/unit/test_skins.py -k fallback`.
- [ ] Appearance hash includes skin; `appearance_hash_for_state` reads it.
  Check: `uv run pytest -q tests/unit/test_skins.py -k appearance`.
- [ ] Inspect prints skin/tokens; doctor covers entry/links/consumer pointers.
  Check: `uv run pytest -q tests/unit/test_skins.py -k inspect`.
- [ ] CLI resolves desired skin (default glass) and wires `SkinExpander` everywhere.
  Check: `uv run pytest -q tests/unit/test_cli_reconcile.py tests/unit/test_cli_crash_recovery.py`.

## 4. Docs / release

- [ ] ARCHITECTURE/README/CHANGELOG document the layer and pointers.
  Check: `make docs-check`.
- [ ] Ticket acceptance ticked + INDEX updated in the same commit.
  Check: `rg "REQ-010" tickets/INDEX.md`.
- [ ] Full gates and annotated `v0.7.0`.
  Check: `make gates && make gates-ci`; `git tag -v v0.7.0`.
