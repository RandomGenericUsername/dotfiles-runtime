---
id: BUG-003
title: "icon-contrast prefs: the loader rejects its own canonical file at 4+ entries"
kind: bug
status: triage
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

- [ ] The current live file parses to four `True` prefs with no warning
      (read-only proof; sha256 unchanged).
- [ ] `parse(serialize(...))` round-trips `0/1/2/3/4/5/10` entries and mixed
      values.
- [ ] `write_pref` four (and ten) times remains readable; no rollback to `{}`.
- [ ] Truly malformed shapes still return `{}` + a warning, and the existing
      negative tests stay green.
- [ ] `make gates` green on the merged ticket branch.
- [ ] The live `~/.local/state/dotfiles/icon-contrast.json` was not edited by
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
