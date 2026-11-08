---
id: BUG-012
title: "Doctor supervision leg compares v2 appearance hashes against the v3 ledger and reports false drift"
kind: bug
status: in-progress
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-012/
branch: ticket/BUG-012-doctor-v3-generation-identity
repro: "uv run dotfiles-runtime doctor --format json | jq -r '.supervision.units[] | select(.stale_appearance == true) | .unit'"
observed: "After a clean v3 apply, both dotfiles-ags-bar.service and dotfiles-ags-notifications.service are stale_appearance=true with detail 'running app predates the current appearance hash', even though appearance-ledger.json records status=applied at generationHash 43d62e17e2da58da2ea43da41bde41fb30e9d0077611a8c34fe30e8d50ef7fc8."
expected: "Doctor compares the ledger against the committed v3 generationHash from current/manifest.json (43d62e17e2da58da2ea43da41bde41fb30e9d0077611a8c34fe30e8d50ef7fc8) and reports both units clean; only an older/non-applied ledger record is drift, and a missing ledger is unknown."
---

# Doctor supervision leg compares v2 appearance hashes against the v3 ledger and reports false drift

## Triage

- `kind: bug` — after a clean v3 apply, `doctor` reports both always-on AGS
  units as stale even though every ledger record is `applied` for the current
  generation, so the desktop never reports clean.
- `lane: 2` — bounded application-layer identity fix plus regression tests; no
  OpenSpec change.
- `severity: high` — the doctor is the health signal for the desktop; a
  guaranteed false drift on every v3 host hides real supervision failures.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` returned 0 on main
  (`636b9cb`) before
  `create BUG-012 doctor-v3-generation-identity`.

## Evidence

- `Evidence:` live state on 2026-11-07:
  `~/.local/state/dotfiles/current -> generations/43d62e17e2da58da2ea43da41bde41fb30e9d0077611a8c34fe30e8d50ef7fc8`,
  and `current/manifest.json` carries
  `"generationHash": "43d62e17e2da58da2ea43da41bde41fb30e9d0077611a8c34fe30e8d50ef7fc8"`.
  `current.json` records palette `0797efd74520459ec24c15a9f7abbbcb62f34a506f7fe371bda3bfb7cabc8363`,
  skin `ed96fe3a571e144a72872ec1067b00d144c57c61aaf6b9859e4fd4fc129ae98e`,
  mode `dark`.
- `Evidence:` `appearance-ledger.json` top-level `appearanceHash` and both
  always-on records (`ags`, `notifications`) are
  `43d62e17e2da58da2ea43da41bde41fb30e9d0077611a8c34fe30e8d50ef7fc8`,
  status `applied`.
- `Evidence:` the v2 recompute from the live palette/skin/mode is
  `09ea6912eae6be3469d972fe1519c1835688e2f495c7318767bd017c3acc69b3`
  (`appearance_generation_hash(palette, skin, mode)`); it does not equal the
  committed generation.
- `Evidence:` `src/runtime/application/doctor.py:641-648` (`_expected_appearance_hash`)
  calls `appearance_generation_hash(...)` unconditionally instead of reading
  the v3 generation identity. `src/runtime/application/inspect.py:238-245`
  already uses `GenerationStore.current_hash()` as the expected identity;
  `src/runtime/adapters/ags_supervision.py:458-470` only compares the values
  it is handed. `src/runtime/adapters/ags_app_refresh.py:537-560` already
  gates on the committed generation hash. `appearance_hash_for_state`
  (`src/runtime/adapters/appearance_ledger.py:71-99`) has no production caller
  and stays the v2 compatibility helper.
- `Evidence:` live `uv run dotfiles-runtime doctor --format json` exits 1 and
  reports `stale_appearance: true`, `"running app predates the current
  appearance hash"` for both `dotfiles-ags-bar.service` and
  `dotfiles-ags-notifications.service`.

## Repro

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
uv run dotfiles-runtime doctor --format json \
  | jq -r '.supervision.units[] | select(.stale_appearance == true) | .unit'
```

## Observed

```text
dotfiles-ags-bar.service
dotfiles-ags-notifications.service
```

`doctor` exits 1 even though `appearance-ledger.json` says both apps are
`applied` at the current generation
`43d62e17e2da58da2ea43da41bde41fb30e9d0077611a8c34fe30e8d50ef7fc8`.

## Expected

- On a valid v3 host, the expected supervision identity is the committed
  generation's `generationHash` read from `current/manifest.json` through the
  same validated `GenerationStore` path the rest of the runtime uses.
- A ledger record with that hash and status `applied` is fresh; an older hash
  or a non-`applied` status is stale; an absent ledger record is unknown, not
  drift.
- A malformed or missing v3 manifest never silently falls back to a v2
  recompute. Only a host with no v3 generation (pre-v3) keeps the v2
  `appearance_generation_hash(palette, skin, mode)` fallback.
- No process-start-time heuristics are introduced.

## Fix notes

Use the committed v3 generation identity in the doctor supervision leg. Read
`GenerationStore.current_hash()` and validate it with `load_manifest` (the
same validation `inspect` uses). If a v3 pointer exists but its manifest is
invalid/missing, return `None` (unknown) and let the existing
`generation:manifest` / `generation:pointer` item carry the defect; never fall
back to the v2 recompute on a v3 host. Fall back to
`appearance_generation_hash(palette, skin, mode)` only when no v3 generation
exists.

Regression tests:

- a v3 current manifest (live fixture values
  `43d62e17…` / `09ea6912…`) makes the probe receive the manifest
  `generationHash`, not the v2 recompute;
- current ledger + current generation is clean, an older hash is
  `supervision:appearance/*` drift, and a missing ledger is unknown/non-drift;
- no v3 manifest still yields the v2 fallback;
- a malformed v3 manifest yields `None`, never a wrong v2 hash.
