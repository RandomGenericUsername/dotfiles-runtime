---
id: REQ-021
title: "wlogout skin consumer runtime regression (W-2.3/W-2.4)"
kind: requirement
status: in-progress
lane: 2 — bounded implementation
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-021/
branch: ticket/REQ-021-wlogout-skin-runtime
---

# wlogout skin consumer runtime regression (W-2.3/W-2.4)

## Why

`dotfiles-assets v0.14.0` shipped `skins/templates/wlogout.gtk.css.j2` and made
`wlogout` a required skin target. The runtime already derives every appearance
target from the `<configDir>.gtk.css.j2` template set, so wlogout should flow
through the existing appearance/generation pipeline with **no production code
change**. The missing piece is the runtime regression proof (W-2.3/W-2.4 in
`dotfiles-v3/docs/108-non-ags-skin-consumers.md`): a hermetic skin/v2 fixture
with wlogout, a committed generation target, and manifest validation against
the embedded contracts v0.8.0 generation schema.

## Requirement

1. Copy the released `skins/templates/wlogout.gtk.css.j2` byte-for-byte from
   `dotfiles-assets` tag `v0.14.0` into
   `tests/fixtures/skins-v2/templates/`.
2. Add regression tests that prove, using the existing runtime code:
   - the skin expansion `config_dirs` include `wlogout` and expose an
     `appearance/wlogout.css` artifact;
   - a generation commit writes `appearance/wlogout.css` and a manifest target
     `{configDir: "wlogout", path: "appearance/wlogout.css"}`;
   - the committed manifest validates against the embedded contracts v0.8.0
     `generation-manifest` schema;
   - the composed wlogout sheet carries the palette and skin sections and the
     skin section has no literal hex, `transition`, structure/variant tokens or
     layout IDs.
3. Do not add a wlogout-specific adapter, do not touch provisioning,
   composition, apps, contracts tags, or live state, and do not run `apply`.
   If a test exposes a genuine pipeline gap, fix only that gap with the
   smallest production change.

## Evidence

- Assets: `dotfiles-assets@v0.14.0` (`696bd901`, tag object) contains
  `skins/templates/wlogout.gtk.css.j2`; the exact blob sha256 is
  `e42a44e61deee2b00801252332abb9a5e4bc2b817db9b727b98ebef6313e32e3`.
- Criteria: `dotfiles-v3/docs/108-non-ags-skin-consumers.md:105-114`
  (W-2.3/W-2.4).
- Runtime target derivation: `src/runtime/domain/models.py:173-187`
  (`skin_config_dirs` reads `<configDir>.css` artifacts, no fixed app list).
- Composition: `src/runtime/adapters/appearance.py:631-688` renders one
  `<configDir>.css` per template; `src/runtime/adapters/generation_store.py:451-467`
  emits one manifest target per `config_dirs` entry.
- Schema: embedded `src/runtime/adapters/schemas/generation-manifest.schema.json`
  is byte-identical to contracts `v0.8.0` (`d86c19c` / `8906f1a`).

## Verify

```sh
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
uv run pytest -q tests/unit/test_wlogout_skin_consumer.py \
  tests/unit/test_appearance_v2_assets.py tests/unit/test_appearance.py \
  tests/unit/test_generations_v3.py
make gates
make gates-ci
make ci-gates-parity
```

## Acceptance

- [x] The wlogout fixture is byte-identical to the released assets v0.14.0 blob.
- [x] The skin `config_dirs` include `wlogout`; `appearance/wlogout.css` exists.
- [x] The committed manifest target is exactly
      `{configDir: "wlogout", path: "appearance/wlogout.css"}`.
- [x] The committed manifest validates against the embedded contracts v0.8.0
      generation schema (W-2.4).
- [x] The composed sheet has palette + skin content; the skin section has no
      literal hex, transition, structure/variant or layout IDs.
- [x] Targeted appearance/generation tests and all gates pass.
- [x] Production code is unchanged unless a real gap is proven and explained.

## Outcome (2026-11-09)

**Worktree:** `.worktrees/REQ-021/`
**Branch:** `ticket/REQ-021-wlogout-skin-runtime`
**Commits:** `f14cd28` (ticket + INDEX plan), `7235e7a` (fixture + regression
tests), this release commit (version/CHANGELOG/README + ticket close).
**Tag:** `v1.0.7` (annotated) after green gates.
**Production code:** unchanged. The existing pipeline already passed; no
wlogout-specific adapter or gap fix was needed.

### Delivered

| Path | What changed |
| --- | --- |
| `tests/fixtures/skins-v2/templates/wlogout.gtk.css.j2` | byte-identical copy of `dotfiles-assets@v0.14.0` blob `e42a44e6…` |
| `tests/unit/test_wlogout_skin_consumer.py` | W-2.3/W-2.4 regression tests: fixture identity, `config_dirs`/artifact, committed target, embedded schema validation, composed palette+skin sheet with no hex/transition/structure/layout IDs |
| `tests/unit/test_appearance_v2_assets.py` | fixture docstring names the v0.14.0 wlogout template alongside the v0.12.0 AGS set |
| `pyproject.toml` / `CHANGELOG.md` / `README.md` | `1.0.7` release metadata |

### Tests

```sh
uv run pytest -q tests/unit/test_wlogout_skin_consumer.py \
  tests/unit/test_appearance_v2_assets.py tests/unit/test_appearance.py \
  tests/unit/test_generations_v3.py tests/unit/test_generation_gc.py \
  tests/unit/test_skins.py tests/unit/test_regenerate_stale.py
# 103 passed in 3.47s
```

### Gates

```sh
make gates
# test: 2439 passed, 3 skipped, 11 deselected
# lint: All checks passed
# contracts-check: 58 passed, 1 skipped
# hermetic: 2439 passed, 3 skipped, 11 deselected
# lock-check: Resolved 25 packages
# docs-check: rc 0
```

`make gates-ci` and `make ci-gates-parity` are recorded on the release commit.
