---
id: BUG-003
title: "icon-contrast prefs: the loader rejects its own canonical file at 4+ entries"
kind: bug
status: done
lane: 1
severity: low
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-003/
branch: ticket/BUG-003-icon-contrast-prefs-malformed
repro: "uv run python -c 'from pathlib import Path; from runtime.adapters.icon_contrast_prefs_store import read_prefs; print(read_prefs(Path.home()/\".local/state/dotfiles/icon-contrast.json\"))' (read-only; the live file is not modified)"
observed: "read_prefs logs 'icon-contrast prefs corrupt (...); using defaults: malformed entries in icon-contrast prefs mapping' and returns {} even though the file is the canonical version-1 shape with four 64-hex keys and boolean values. Serialize(parse(...)) round-trips for 1-3 entries and fails for 4-6; write_pref called four times writes a file that read_prefs then rejects."
expected: "The store accepts its own canonical version-1 document for any number of prefs, including the current four-entry live file, returns the four booleans with no warning, and keeps per-wallpaper contrast choices persistent. Truly malformed files still fall back to defaults with a clear warning, and a regression test pins 0/1/2/3/4/5/N-entry round-trips plus the write-then-read path. No live-file edit or data migration is required for this defect."
---

# icon-contrast prefs: the loader rejects its own canonical file at 4+ entries

## Triage (`/triage`, 2026-10-26)

- `kind: bug` — the store's writer and reader disagree. `write_pref` emits the
  documented canonical shape, and `read_prefs` rejects it once the mapping has
  four entries. This is a code defect in the read-path validator, not a
  one-time data migration; an ops-task/reset would hide the bug and lose the
  user's existing choices.
- `lane: 1 — XS direct` — one localized pure-domain predicate fix plus a
  deterministic regression test; fully understood, no OpenSpec change, no
  architecture decision, no cross-repo dependency.
- `severity: low` — no `wallpaper set`/`reconcile` failure: the pipeline falls
  back to the default-ON guard and the selector still works. The user-visible
  impact is that per-wallpaper contrast choices silently stop persisting once
  four hashes are stored (and every later write may reset the map to one
  entry). That is annoying data loss, not a broken desktop.
- `Evidence:` `src/runtime/domain/icon_contrast_policy.py:74-76` (remainder
  check), `:80-94` (`serialize` emits the same shape),
  `src/runtime/adapters/icon_contrast_prefs_store.py:51-72` (tolerant read /
  warning), `:75-105` (`write_pref` read-modify-write),
  `tests/unit/test_icon_contrast_policy.py:17-25` (round-trip test only uses
  two entries), `tests/unit/test_icon_contrast_prefs_store.py:38-41` (store
  round-trip only one entry).
- `Verify:` `uv run python -c 'from pathlib import Path; from runtime.adapters.icon_contrast_prefs_store import read_prefs; print(read_prefs(Path.home()/".local/state/dotfiles/icon-contrast.json"))'`
  → warning + `{}`.

## Repro

Read-only, against the current live file (no edit):

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
uv run python - <<'PY'
from pathlib import Path
from runtime.adapters.icon_contrast_prefs_store import read_prefs
f = Path.home() / ".local/state/dotfiles/icon-contrast.json"
print("sha256_input_bytes=", __import__("hashlib").sha256(f.read_bytes()).hexdigest())
print("parsed=", read_prefs(f))
PY
```

Deterministic, no host state:

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
uv run python - <<'PY'
from runtime.domain.icon_contrast_policy import parse, serialize
for n in range(1, 7):
    prefs = {f"{i:064x}": True for i in range(n)}
    try:
        print(n, "OK", len(parse(serialize(prefs))))
    except ValueError as exc:
        print(n, "FAIL", exc)
PY
```

Write-then-read proof (this is the real regression; use a temp dir, never the
live file):

```bash
uv run python - <<'PY'
import tempfile
from pathlib import Path
from runtime.adapters.icon_contrast_prefs_store import read_prefs, write_pref
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "icon-contrast.json"
    for i in range(4):
        write_pref(p, f"{i:064x}", True)
    print(p.read_text().strip())
    print("read_prefs ->", read_prefs(p))
PY
```

## Observed

- Live file `/home/inumaki/.local/state/dotfiles/icon-contrast.json`
  (322 bytes, mtime `2026-10-26 08:30:17 -0500`, sha256
  `19bb2f4d08c540a8c923078d43cedfa4c0690092a344b71f1a6e86f12582a508`):
  `{"version": 1, "prefs": {"22b0…fc46": true, "5c6a…7f61": true,
  "8b3a…c6eb": true, "b885…fafc": true}}` — four 64-hex keys, all boolean
  `true`.
- `read_prefs` on that file logs:
  `icon-contrast prefs corrupt (/home/inumaki/.local/state/dotfiles/icon-contrast.json); using defaults: malformed entries in icon-contrast prefs mapping`
  and returns `{}`.
- The round-trip size sweep prints `1 OK`, `2 OK`, `3 OK`, `4 FAIL`,
  `5 FAIL`, `6 FAIL` with `malformed entries in icon-contrast prefs mapping`.
- `write_pref` four times produces the same canonical four-entry text, then
  `read_prefs` returns `{}` and logs the same warning.
- The file's version is `1`, equal to `PREFS_VERSION`; every key is 64-hex and
  every value is a JSON boolean. It is not a schema-version mismatch, not a
  key/value shape mismatch, and not a write-path shape bug. The parser's
  remainder validation is wrong.

### Root cause (evidence-backed)

`parse` removes every matched `"<hex>": true|false` entry from the prefs body
and validates the remainder with:

```python
remainder = _ENTRY_RE.sub("", body)
if remainder.strip().strip(",").strip():
    raise ValueError("malformed entries in icon-contrast prefs mapping")
```

For `n` entries the canonical serializer leaves `n-1` comma-space separators.
With four entries the remainder is `", , , "`:

1. `.strip()` → `", , ,"`;
2. `.strip(",")` removes only the first and last commas → `" , "`;
3. `.strip()` → `","` — truthy, so the file is rejected.

Three or fewer entries happen to leave no interior comma after step 2; four
and above always do. The check conflates "only separators/whitespace remain"
with "a comma remains", and the error message names the wrong thing
("malformed entries"). The existing round-trip tests never crossed the
four-entry boundary, so this survived.

## Expected

- `read_prefs` accepts the current live file and returns the four stored
  booleans (`True` each), with no `corrupt` warning.
- `parse(serialize(prefs))` round-trips for every realistic count, including
  `0`, `1`, `2`, `3`, `4`, `5`, and a larger map (e.g. 10 or 25).
- `write_pref` remains read-modify-write: after the fourth, fifth, and tenth
  distinct hashes, `read_prefs` still returns all entries.
- Genuinely malformed documents still degrade safely to `{}` + a clear warning:
  wrong version, non-object, missing `prefs`, short/non-hex keys, non-boolean
  values, trailing non-comma garbage, and trailing commas if they are not part
  of the accepted grammar.
- No live-file edit, no reset, and no migration are required: once the parser
  is fixed the existing file is valid and the user's four choices persist. A
  migration/reset path is only proposed for future schema changes (see below).

## Investigation / fix plan (next session)

1. **Reproduce red (10 min):** run the three commands above in the BUG-003
   worktree; save output under `/tmp/opencode/bug003-*.txt`. Confirm the live
   file sha256 is unchanged before/after (read-only repro).
2. **Fix the validator (30 min):** replace the ad-hoc `strip(",")`
   remainder check with a grammar-correct one, e.g. remove separators and
   whitespace, or validate only that the remainder consists of `[\s,]*` after
   entry removal (`remainder.strip(" \t\r\n,") == ""`), or split the body on
   commas and require every non-empty part to match a single entry. Keep
   rejecting empty keys, non-hex keys, non-bool values, and trailing garbage.
   Do not weaken `serialize`; it already emits the canonical shape.
3. **Regression tests (30 min):** extend
   `tests/unit/test_icon_contrast_policy.py` with a parametrized
   `parse(serialize(...))` sweep over `0,1,2,3,4,5,10` entries (and mixed
   true/false / uppercase keys); extend
   `tests/unit/test_icon_contrast_prefs_store.py` with a
   `write_pref × 4` then `read_prefs` test and a "current live shape"
   fixture (inline four 64-hex keys, not a host read). Keep the negative
   malformed-shape cases.
4. **No live-file change:** verify `sha256sum` is unchanged across the whole
   session. The first successful `write_pref` after the fix will rewrite the
   same logical map in canonical form; that is fine, but it is a user action,
   not this ticket's test.
5. **Gates:** `uv run pytest -q tests/unit/test_icon_contrast_policy.py
   tests/unit/test_icon_contrast_prefs_store.py`, then `make gates`; merge only
   when green. This is a docs-only triage commit now; implementation is a
   separate commit/turn.

## Implementation session (2026-10-26)

- Worktree: `.worktrees/BUG-003/`; branch:
  `ticket/BUG-003-icon-contrast-prefs-malformed` (fast-forwarded to `main`
  `ad9a61c` before implementation).
- Red: `a74d8aa test(BUG-003): pin 4+ entry icon-contrast prefs round-trip`.
  `uv run pytest -q tests/unit/test_icon_contrast_policy.py
  tests/unit/test_icon_contrast_prefs_store.py` → `8 failed, 68 passed`; the
  failures are the 4/5/10-entry round-trips, the four-entry canonical read, and
  `write_pref` ×4/×5/×10. Log: `/tmp/opencode/bug003-red-tests.txt`.
- Fix: `33ba8bf fix(BUG-003): accept canonical 4+ entry icon-contrast prefs`.
  After entry removal the parser now requires the remainder to match
  `[\s,]*` instead of peeling only outer commas; any other residual content
  still raises `ValueError`.
- Green: the same focused command → `76 passed in 0.09s`; read-only live-file
  repro returns the four `True` prefs with sha256 `19bb2f4d…` unchanged;
  `write_pref` ×10 reads back all 10 entries. Logs:
  `/tmp/opencode/bug003-green-focused.txt`,
  `/tmp/opencode/bug003-repro-after.txt`,
  `/tmp/opencode/bug003-write10-after.txt`.
- Gates on the ticket branch: `make gates` → exit 0; `2150 passed, 3 skipped,
  21 deselected`; `lint`, `contracts-check`, `hermetic`, `lock-check` and
  `docs-check` green. Log: `/tmp/opencode/bug003-gates-branch.txt`.
- **Release pending:** merge to `main` only. No tag, no release, no version or
  CHANGELOG bump. `v0.2.2` is held until the user decides whether BUG-004
  (already in `main`/`v0.2.1`) is kept or reverted.

### Merge / release pending (2026-10-26)

- Merged `ticket/BUG-003-icon-contrast-prefs-malformed` into `main` as merge
  commit `a259fe8` (parents `ad9a61c` and `9f8412f`).
- `make gates` on the merged `main` → exit 0; `2151 passed, 2 skipped,
  21 deselected`; `lint`, `contracts-check`, `hermetic`, `lock-check` and
  `docs-check` green. Log: `/tmp/opencode/bug003-gates-merged.txt`.
- Live file sha256 stayed
  `19bb2f4d08c540a8c923078d43cedfa4c0690092a344b71f1a6e86f12582a508`
  across the whole session; no migration, reset or host-file edit.
- **No release was cut.** `v0.2.2` remains held until the user decides whether
  BUG-004 (already in `main`/`v0.2.1`) is kept or reverted; this ticket makes
  no tag, version, CHANGELOG or GitHub-release change.

### Released in v0.2.2 (2026-10-26)

- User decision: **keep BUG-004** (the btop-theme-studio skip); no revert.
- Release prep commit `5fff96e` (`chore(release): bump dotfiles-runtime to
  0.2.2`): `pyproject.toml`/`uv.lock` `0.2.2`, `runtime.__version__ = "0.2.2"`
  (the requested `src/provisioning/__init__.py` does not exist in this repo;
  the package `__init__` is `src/runtime/__init__.py`), and the CHANGELOG
  0.2.2 entry naming BUG-003, retained BUG-004 and the included v0.2.1
  BUG-002 work.
- `make gates` on `5fff96e` → exit **0**: test `2151 passed, 2 skipped,
  21 deselected`; lint all checks passed; contracts-check `40 passed`;
  hermetic `2151 passed, 2 skipped, 21 deselected`; lock-check resolved 25
  packages; docs-check green. Log: `/tmp/opencode/v0.2.2-gates.txt`.
- Annotated tag **`v0.2.2`** → tag object
  `2ce4bd1872a26b60c9e41f7dcf5fe476cb8ee477`, commit
  `5fff96e7ce3c240e8b4aff2ab7097a68104fd529`.
- Tag CI run `40403249834`: `success`; all eight jobs succeeded, including
  `release`.
- GitHub Release <https://github.com/RandomGenericUsername/dotfiles-runtime/releases/tag/v0.2.2>
  published with `dotfiles_runtime-0.2.2-py3-none-any.whl` and
  `dotfiles_runtime-0.2.2.tar.gz`.

`Verify:` `git rev-parse v0.2.2 'v0.2.2^{commit}'`;
`gh release view v0.2.2 --json assets --jq '.assets[].name'`;
`gh run view 40403249834 --json conclusion`; `make gates`.

### Migration / reset decision

**No migration or reset for this defect.** The live document is already the
current schema (`version: 1`, 64-hex keys, bool values); the reader is wrong.
A reset would destroy four valid user choices and would not prevent the next
four-entry write from poisoning the file again. If a genuinely unsupported
future version appears, the existing contract (fallback to `{}` + warning) is
acceptable; a later hardening could quarantine an unreadable file
(`icon-contrast.json.corrupt-<timestamp>`) instead of silently overwriting it
on the next `write_pref`, but that is a separate improvement, not this fix.

## Acceptance

- [x] The current live file parses to four `True` prefs with no warning
      (read-only proof; sha256 unchanged).
- [x] `parse(serialize(...))` round-trips `0/1/2/3/4/5/10` entries and mixed
      values.
- [x] `write_pref` four (and ten) times remains readable; no rollback to `{}`.
- [x] Truly malformed shapes still return `{}` + a warning, and the existing
      negative tests stay green.
- [x] `make gates` green on the merged ticket branch.
- [x] The live `~/.local/state/dotfiles/icon-contrast.json` was not edited by
      the triage or implementation session.

## Scope / constraints

- No production code or live-file change in this triage step; no sudo, no
  host changes.
- Do not conflate this with the palette/csg investigation (BUG-002); it is a
  separate prefs-store defect.
- The store's tolerant-read contract stays: a damaged file must never break
  icon derivation. The fix only makes the tolerance predicate correct.
- `domain/` purity stays: no `json` import or file I/O in
  `icon_contrast_policy.py`; the regex-subset reader remains the documented
  approach.

## Next-session starting points

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
git worktree list                      # .worktrees/BUG-003/ on ticket/BUG-003-icon-contrast-prefs-malformed
git switch ticket/BUG-003-icon-contrast-prefs-malformed
sed -n '42,95p' src/runtime/domain/icon_contrast_policy.py
sed -n '45,110p' src/runtime/adapters/icon_contrast_prefs_store.py
sed -n '1,80p' tests/unit/test_icon_contrast_policy.py
sed -n '34,90p' tests/unit/test_icon_contrast_prefs_store.py
uv run pytest -q tests/unit/test_icon_contrast_policy.py tests/unit/test_icon_contrast_prefs_store.py
```

Read `/home/inumaki/.opencode/plan/14-HANDOFF-icon-contrast-prefs.md` and this
ticket before touching code. Do not open or rewrite the live file; use
`tmp_path` and inline fixtures.
