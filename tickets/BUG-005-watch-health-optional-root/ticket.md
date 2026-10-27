---
id: BUG-005
title: "watch-health reports degraded when the optional desired-state root has no parent"
kind: bug
status: done
lane: 2
severity: low
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-005/
branch: ticket/BUG-005-watch-health-optional-root
repro: "cat ~/.local/state/dotfiles/watch-health.json; dotfiles-runtime inspect daemon; journalctl --user -u dotfiles-runtime-daemon.service -n 40 --no-pager | grep 'parent missing'"
observed: "watch-health.json (175 bytes, sha256 24f04324…) is {\"version\": 1, \"degraded\": true, \"failed_roots\": [\"/home/inumaki/.config/dotfiles/desired.json\"], \"last_error\": \"parent missing\", \"updated_at\": \"2026-10-26T22:36:58.878520Z\"}; inspect daemon prints 'watch health: degraded (1 unwatched): parent missing' and 'UNWATCHED /home/inumaki/.config/dotfiles/desired.json'; every daemon start logs 'watch: file root parent missing, not watched: /home/inumaki/.config/dotfiles/desired.json'; ~/.config/dotfiles does not exist; the daemon is present and all four required spine roots are watched."
expected: "The absent optional desired-state root is reported as optional/absent/not-watched with degraded: false and failed_roots: []; only the four required spine roots can mark the watch set degraded. inspect daemon/doctor show the optional root as informational and exit 0. If triage instead chooses provisioning, ~/.config/dotfiles exists so the file root is watched and this repro no longer occurs; that alternative is rejected below in favour of optional-root semantics."
---

# watch-health reports degraded when the optional desired-state root has no parent

## Triage (`/triage`, 2026-10-26)

- `kind: bug` — the health surface misclassifies a documented optional input's
  absence as watch loss. `desired.json` is optional by contract (absent file →
  `None`), but a missing **parent directory** is recorded as a failed required
  root and flips the whole watch set to `degraded: true`. This is a
  health-reporting/semantics defect, not a missing feature.
- `lane: 2 — one bounded fix` — the change touches the watch-root metadata, the
  in-process `WatchStatus`, the persisted `watch-health.json` record, and the
  read-only `inspect`/`doctor` rendering, plus one retry rule and focused
  regression tests. It is fully understood and does not change the D-Bus
  contract, the AD-39 root set, or any other repo. No separate OpenSpec change
  is opened for this defect (same pattern as BUG-001/BUG-002).
- `severity: low` — there is no functional impact today: the daemon is present,
  all four required spine roots are watched, `inspect daemon`/`doctor` exit 0,
  and no desktop state is changed. The defect is a false-positive health signal
  plus a warning on every daemon start; it can obscure a real watch loss and
  makes the diagnostic surface untrustworthy. It is not medium because nothing
  fails, nothing is lost, and no user-visible path is blocked.
- `Guard:` `bash scripts/worktree-guard.sh check-clean` exited 0 before this
  worktree was created.
- `Evidence:` optional-reader contract at
  `src/runtime/adapters/desired_state_reader.py:14-15,51-63`; stable absent
  sentinel in the backstop hash at
  `src/runtime/adapters/converge_inputs.py:8-12,36-41`; the intent root has no
  optional marker at `src/runtime/adapters/watch_roots.py:27-33,64`; the
  missing-parent failure at
  `src/runtime/adapters/inotify_watch_source.py:208-222` and the recording path
  at `:189-192`; `degraded` is “any failed root” at
  `src/runtime/domain/watch.py:62-81`; the record is persisted from that status
  at `src/runtime/adapters/watch_health.py:113-122`; the CLI renders it at
  `src/runtime/cli/main.py:3571-3579` and `:1811-1822`; the current test pins
  the buggy outcome at
  `tests/unit/test_inotify_registration_recovery.py:208-226`.
- `Verify:` the `repro` command above; the deterministic local proof in
  “Repro” below prints
  `WatchStatus(registered=0, failed=('/tmp/…/desired.json',), last_error='parent missing')`
  on the current tree.

## Repro

Live, read-only (do not edit the file or the config tree):

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
cat ~/.local/state/dotfiles/watch-health.json
dotfiles-runtime inspect daemon
journalctl --user -u dotfiles-runtime-daemon.service -n 40 --no-pager | grep 'parent missing'
```

Deterministic, no host state (the same failure without the daemon):

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
uv run python - <<'PY'
import tempfile
from pathlib import Path
from runtime.adapters.inotify_watch_source import InotifyWatchSource
from runtime.adapters.watch_roots import WatchRoot

with tempfile.TemporaryDirectory() as d:
    intent = Path(d) / "config" / "dotfiles" / "desired.json"  # parent absent
    source = InotifyWatchSource((WatchRoot(intent, is_directory=False),))
    source.start()
    try:
        print(source.status(), "degraded=", source.status().degraded)
    finally:
        source.close()
PY
```

## Observed

- Live record `/home/inumaki/.local/state/dotfiles/watch-health.json` (175
  bytes, sha256 `24f04324d885ebd32893ca08d50ce5b226c1fe1944f6490636f6dc3ccc12f67e`):
  `{"version": 1, "degraded": true, "failed_roots":
  ["/home/inumaki/.config/dotfiles/desired.json"], "last_error": "parent
  missing", "updated_at": "2026-10-26T22:36:58.878520Z"}`.
- `dotfiles-runtime inspect daemon` (exit 0, daemon present, epoch 1, one active
  clipboard job):
  `watch health: degraded (1 unwatched): parent missing` followed by
  `  UNWATCHED /home/inumaki/.config/dotfiles/desired.json`.
- Journal, repeated on every daemon start:
  `WARNING runtime.adapters.inotify_watch_source: watch: file root parent
  missing, not watched: /home/inumaki/.config/dotfiles/desired.json`.
- `~/.config/dotfiles` is absent (`ls` exits 2). The four required roots under
  `~/.local/share/dotfiles` exist and are watched.
- Deterministic proof on the current tree: the temporary
  `…/config/dotfiles/desired.json` root yields
  `WatchStatus(registered=0, failed=('…/desired.json',), last_error='parent
  missing')` and `degraded=True`.
- Control case: if the parent exists but `desired.json` does not, the source
  already reports `registered=1, failed=(), degraded=False` (it watches the
  parent filtered to the exact filename). Only the **missing parent** case is
  misclassified.

## Root cause

`WatchRoot` has no way to say “this input is optional”
(`src/runtime/adapters/watch_roots.py:27-33`); `enumerate_watch_roots` builds
the intent root exactly like the four required roots (`:64`). When the parent
directory is absent, `_install_file_root` assumes a lost root and calls
`_record_unwatchable(path, "parent missing")`
(`src/runtime/adapters/inotify_watch_source.py:215-222`). `WatchStatus.degraded`
is then `bool(self.failed)` (`src/runtime/domain/watch.py:78-81`), and
`WatchHealthStore.write` persists that as the health record
(`src/runtime/adapters/watch_health.py:113-122`). The intent document is
explicitly optional: the reader returns `None` when it is absent
(`src/runtime/adapters/desired_state_reader.py:14-15,51-63`) and the backstop
hash uses a stable `__absent__` sentinel whose value changes once the file
appears (`src/runtime/adapters/converge_inputs.py:8-12,36-41`). Absence is
therefore an expected input state, not watch loss.

The existing regression test
`tests/unit/test_inotify_registration_recovery.py:208-226`
(`TestMissingRootWarnOnce.test_missing_parent_warns_once_across_rebuilds`)
asserts `source.status().degraded is True` for the missing-parent case, so it
currently pins the defect rather than the contract.

## Expected semantics (chosen fix)

1. The intent document remains optional. An absent `desired.json` **and** an
   absent `~/.config/dotfiles` are both “optional root absent, not watched”, not
   degraded.
2. `degraded` is true only when a **required** root cannot be watched (missing
   required path, wrong kind, registration exhaustion, `IN_IGNORED` loss, …).
3. The health surface must say what happened without alarm:
   `degraded: false`, `failed_roots: []`, and an additive
   `optional_absent: ["…/desired.json"]` (or equivalent wording) in the
   persisted record and the `inspect daemon`/`doctor` JSON. Plain output prints
   an informational `OPTIONAL (absent, not watched)` line; `inspect`/`doctor`
   keep exit 0.
4. The missing-parent optional root is still retried at the next safe
   opportunity (an arriving event from a watched root — never a timer/poll), so
   if the user creates the parent and `desired.json` later, the watch is
   installed without a daemon restart. This preserves the current recovery
   behaviour while removing the false degradation.
5. No live-file edit or migration is required. The current live record stays as
   it is until the upgraded daemon starts and rewrites it; a read-only
   verification must not modify it.

## Design options and recommendation

### Option A — optional-root semantics (recommended)

Add `optional: bool = False` to `WatchRoot`, mark only the intent root optional,
and carry an `optional_absent` tuple through `WatchStatus` →
`WatchHealthRecord` → `DaemonWatchHealthView` → `inspect`/`doctor`. Keep
`WatchStatus.degraded == bool(failed)` so required failures alone degrade the
set. Log the optional absence at INFO once per root (not WARNING), and have
`WatchCoordinator` retry when `status.degraded or status.optional_absent` once
per burst. The `watch-health.json` change is additive; old records without the
key read as an empty tuple and old readers ignore the new key, so
`WATCH_HEALTH_VERSION` can stay `1`.

- Pros: fixes the general class (not a path special-case), preserves future
  detection of the optional file, keeps the four required roots strict, no
  D-Bus/contract change, no cross-repo dependency, testable hermetically.
- Cons: touches five small modules plus tests; adds one persisted field.
- Evidence: the optional-reader contract and absent sentinel cited above; the
  control case (parent present, file absent) already behaves correctly, so only
  optionality metadata is missing.

### Option B — provision `~/.config/dotfiles` (chosen only as a follow-up, not the fix)

Have `dotfiles-provision` create the parent directory (mode `0700`) during
install so the file root is immediately watchable. This removes the repro on a
freshly provisioned host.

- Pros: no runtime data-model change; `desired.json` creation is watched from
  the start.
- Cons: cross-repo change and release/pin chain; existing hosts need a
  re-provision; a user deleting `~/.config/dotfiles`, a custom
  `XDG_CONFIG_HOME`, or a missing provision step still produces the false
  degradation. It also makes a health signal depend on provisioning instead of
  the runtime's own documented input contract. Not recommended as the sole fix.
- A runtime-side parent creation is rejected: readers do not provision, and the
  runtime must not write a watched root or mutate user config.

### Option C — silently ignore a missing parent (rejected)

Drop the failure without tracking it. This is the smallest diff but it silently
leaves a documented root unwatched and does not even attempt recovery when the
parent appears, which is exactly what AD-40 forbids. It would also make the
“optional absent” state invisible instead of explicit.

### Option D — special-case the literal `desired.json` path (rejected)

Matching on the path or filename keeps `WatchRoot` unchanged, but it couples the
generic watch source to one document and would grow another exception for every
future optional input. Explicit optional metadata is cheaper to reason about and
to test.

**Recommendation: Option A.** It fixes the semantics at the source of truth,
keeps required-root health strict, and makes the absence observable without
noise. Option B may be added later as provisioning hardening (create the config
parent on fresh installs) but is not needed for the health contract and is a
separate cross-repo change. The 2026-10-26 `/triage` decision is A; the
provisioning variant is documented for completeness only.

## Implementation plan (next session)

1. **Watch-root metadata** — `src/runtime/adapters/watch_roots.py`:
   add `optional: bool = False` as the last field of `WatchRoot` (existing
   positional construction stays valid), document it, and construct the intent
   root as `WatchRoot(intent_path, is_directory=False, optional=True)`. Do not
   change the four required roots or their order.
2. **Status model** — `src/runtime/domain/watch.py`: add
   `optional_absent: tuple[str, ...] = ()` to `WatchStatus`; keep
   `degraded = bool(self.failed)` and update the docstring so `failed` means
   required roots only and `optional_absent` is informational/retryable.
3. **Inotify source** — `src/runtime/adapters/inotify_watch_source.py`: track
   optional absences separately from `_failed`; on a missing optional
   file/directory root, record the path in `_optional_absent` and log at INFO
   once per root (not WARNING), and do not call `_record_unwatchable`; on a
   successful install, discard the optional marker and re-arm the log. Clear
   the set in `rebuild()` and `close()`, and include it (sorted) in `status()`.
   Keep the required-root warning/failure path byte-for-byte compatible.
4. **Health record** — `src/runtime/adapters/watch_health.py`: add
   `optional_absent: tuple[str, ...] = ()` to `WatchHealthRecord`; write the
   `optional_absent` JSON key; read it as an optional `list[str]` (missing key →
   `()`, wrong shape → `None` like the other fields). Keep `version: 1` and
   document the additive field.
5. **Status view** — `src/runtime/adapters/daemon_status.py`: add
   `optional_absent` to `DaemonWatchHealthView` and pass it through
   `assemble_daemon_report`.
6. **CLI rendering** — `src/runtime/cli/main.py`: include
   `optional_absent` in the `inspect daemon` and `doctor` JSON; in plain output
   print the optional roots as informational (`OPTIONAL (absent, not watched)
   <path>`) and keep `degraded`/`UNWATCHED` exclusively for required failures.
   Do not change exit codes.
7. **Coordinator retry** — `src/runtime/application/watch.py`: keep the
   once-per-burst retry condition as
   `status.degraded or status.optional_absent`; `_report_status` already
   suppresses unchanged-status writes, so a permanently absent optional root
   cannot create a write loop.
8. **Tests** — add/update the focused tests listed under “Tests and
   acceptance”. Split the existing missing-parent test into an optional case
   (not degraded, INFO, no `UNWATCHED`) and a required case (degraded,
   WARNING, `failed`). Keep the existing exhaustion/IN_IGNORED tests green.
9. **Gates** — run the focused tests, then `make gates` on the ticket branch.
   Merge only when green. No tag, release, version bump, CHANGELOG edit, or
   other-repo change in the implementation ticket; those are the follow-up
   chain below.

### Suggested shape (illustrative, not applied in this triage)

```python
@dataclass(frozen=True, slots=True)
class WatchRoot:
    path: Path
    is_directory: bool
    depth: int = 0
    optional: bool = False

@dataclass(frozen=True, slots=True)
class WatchStatus:
    registered: int = 0
    failed: tuple[str, ...] = ()
    last_error: str | None = None
    optional_absent: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.failed)  # required roots only
```

## Tests and acceptance

Focused tests to add or update:

- `tests/unit/test_watch_roots.py`: the intent root has `optional is True`; the
  four spine roots remain `optional is False`; the order/depths/exclusions stay
  unchanged.
- `tests/unit/test_inotify_registration_recovery.py`:
  `test_missing_optional_parent_is_not_degraded` — missing-parent optional root
  → `degraded is False`, `failed == ()`, `optional_absent == (path,)`, no
  WARNING; `test_missing_required_parent_is_degraded` — required root keeps the
  current `failed`/`degraded`/WARNING behavior (warn once across rebuilds).
  Include the control case “parent exists, file absent” → `degraded is False`
  and one registered watch.
- `tests/unit/test_watch_health.py`:
  `test_roundtrip_optional_absent`; `test_legacy_record_without_optional_absent`
  (hand-written v1 record without the key → `()`); invalid
  `optional_absent` shape → `None`; existing corruption/symlink/atomic tests
  stay green.
- `tests/unit/test_watch_coordinator.py` (or the registration-recovery test):
  an optional-absent status triggers exactly one `rebuild` in a burst; once the
  source reports the watch installed, the next burst does not rebuild; a
  healthy status never rebuilds.
- `tests/unit/test_cli_inspect_daemon.py`:
  `test_optional_absent_is_reported_not_degraded` (JSON `degraded: false`,
  `failed_roots: []`, `optional_absent: [path]`; plain output contains
  `watch health: ok` and `OPTIONAL (absent, not watched)`); existing degraded
  and healthy tests remain green.
- `tests/unit/test_doctor_check.py`: optional absent is informational, doctor
  exits 0, JSON carries `optional_absent`; required failed roots still render
  degraded and do not change the exit code (existing behavior).
- Optional but recommended: `tests/unit/test_daemon_run.py` — the persisted
  record from an injected optional-absent source contains `optional_absent` and
  `degraded: false`.

Acceptance:

- [x] The deterministic repro prints `degraded=False`, `failed=()`, and
      `optional_absent` naming the missing-parent intent root.
- [x] A missing required root still prints `degraded=True` with `failed`
      naming it; exhaustion/`IN_IGNORED` behavior is unchanged.
- [x] `inspect daemon` and `doctor` show the optional root as informational,
      exit 0, and JSON includes the additive field; the degraded/UNWATCHED
      wording is reserved for required roots.
- [x] The empty-parent optional root is retried once per burst and becomes
      watched after the parent is created (no polling, no restart required in
      the test).
- [x] Old `watch-health.json` records without `optional_absent` still read
      (empty tuple), and corrupt/symlinked records still read as `None`.
- [x] `make gates` is green on the ticket branch.
- [x] The live `~/.local/state/dotfiles/watch-health.json` and
      `~/.config/dotfiles` were not edited during triage or implementation;
      the live record only changes when the upgraded daemon next writes it.

## Implementation (2026-10-26)

Worktree/branch: `.worktrees/BUG-005/` on
`ticket/BUG-005-watch-health-optional-root`; implementation commit `ee0556b`
(`fix(BUG-005): keep absent optional watch roots informational`).

What changed:

- `WatchRoot.optional` (last field, default `False`); only the intent root is
  constructed with `optional=True` in `enumerate_watch_roots`.
- `WatchStatus.optional_absent`; `degraded` remains `bool(self.failed)` so only
  required roots degrade.
- `InotifyWatchSource` tracks an absent optional root separately, logs the
  absence at INFO once per root, clears it on install/rebuild/close, and exposes
  it sorted in `status()`. Required roots keep the WARNING/`failed` path.
- `WatchHealthRecord.optional_absent` is persisted as an additive `list[str]`
  under the unchanged `version: 1`; missing key → `()`, wrong shape → `None`.
- `DaemonWatchHealthView`/`assemble_daemon_report` carry the field; `inspect
  daemon` and `doctor` include it in JSON and print
  `OPTIONAL (absent, not watched) <path>` while keeping exit 0.
- `WatchCoordinator` retries once per burst when
  `status.degraded or status.optional_absent`.

Post-fix deterministic proof (uses the real enumerated root set; the triage
snippet above constructs a bare required `WatchRoot`, so it intentionally keeps
the required-root behavior):

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
uv run python - <<'PY'
import tempfile
from pathlib import Path
from runtime.adapters.inotify_watch_source import InotifyWatchSource
from runtime.adapters.watch_roots import enumerate_watch_roots

with tempfile.TemporaryDirectory() as d:
    spine = Path(d) / "install"
    (spine / "config" / "color-scheme-generator" / "templates").mkdir(parents=True)
    (spine / "config" / "weg").mkdir(parents=True)
    (spine / "config" / "weg" / "effects.yaml").touch()
    (spine / "icon-templates").mkdir()
    (spine / "icon-mappings").mkdir()
    intent = Path(d) / "config" / "dotfiles" / "desired.json"  # parent absent
    source = InotifyWatchSource(enumerate_watch_roots(spine, intent))
    source.start()
    try:
        status = source.status()
        print(status, "degraded=", status.degraded)
    finally:
        source.close()
PY
```

Observed:
`WatchStatus(registered=4, failed=(), last_error=None,
optional_absent=('<tmp>/config/dotfiles/desired.json',)) degraded= False`.
A temporary required root with no parent prints `degraded=True`,
`failed=('<tmp>/spine/weg/effects.yaml',)`; the control case (parent present,
file absent) prints `registered=1`, `failed=()`, `optional_absent=()`.

- `Evidence:` focused tests
  `uv run pytest -q tests/unit/test_watch_roots.py
  tests/unit/test_inotify_registration_recovery.py tests/unit/test_watch_health.py
  tests/unit/test_watch_coordinator.py tests/unit/test_cli_inspect_daemon.py
  tests/unit/test_doctor_check.py tests/unit/test_daemon_run.py`
  → 135 passed. `make gates` → exit 0 (test 2167 passed, 3 skipped, 21
  deselected; lint, contracts-check, hermetic, lock-check, docs-check green).
  Temp-XDG `inspect daemon` → exit 0 with `watch health: ok` and
  `OPTIONAL (absent, not watched) …`; JSON `degraded: false`, `failed_roots:
  []`, `optional_absent: [path]`. Doctor test covers the same exit-0/JSON path.
- `Evidence:` live file untouched — `175` bytes, sha256
  `24f04324d885ebd32893ca08d50ce5b226c1fe1944f6490636f6dc3ccc12f67e`
  (same as Observed); `~/.config/dotfiles` still absent.
- `Verify:` rerun the focused test command above, then `make gates`.
- No tag, release, version bump, CHANGELOG edit, sudo, host change, live-file
  edit, or other-repo change was made.

## Released in v0.2.3 (2026-10-26)

- Release-prep commit `d0e37e9` (`chore(release): bump dotfiles-runtime to
  0.2.3`): `pyproject.toml`/`uv.lock` `0.2.3`,
  `runtime.__version__ = "0.2.3"`, and the CHANGELOG 0.2.3 entry naming
  BUG-005 (optional absence informational, additive `optional_absent` under
  version 1, `OPTIONAL (absent, not watched)` with exit 0, once-per-burst
  retry, required-root behavior unchanged).
- `make gates` on `d0e37e9` → exit **0**: test `2168 passed, 2 skipped, 21
  deselected`; lint all checks passed; contracts-check `40 passed`; hermetic
  `2168 passed, 2 skipped, 21 deselected`; lock-check resolved 25 packages;
  docs-check green. Log: `/tmp/opencode/v0.2.3-gates.txt`.
- `make build` → exit **0**: `dotfiles_runtime-0.2.3.tar.gz` and
  `dotfiles_runtime-0.2.3-py3-none-any.whl` in `dist/`; `uv run
  dotfiles-runtime version` → `0.2.3`, exit **0**. Log:
  `/tmp/opencode/v0.2.3-build.txt`.
- Annotated tag **`v0.2.3`** → tag object
  `960257251af75964c34a825fa64fb4106836faba`, peeled commit
  `d0e37e91c88371bdd442278408680cec0c334cf4`. Pushed; never moved or deleted.
- Tag CI run
  [40428185474](https://github.com/RandomGenericUsername/dotfiles-runtime/actions/runs/40428185474)
  → `success`; all eight jobs succeeded (`test`, `lint`, `contracts-check`,
  `hermetic`, `lock-check`, `docs-check`, `ci-gates-parity`, `release`).
- GitHub Release
  <https://github.com/RandomGenericUsername/dotfiles-runtime/releases/tag/v0.2.3>
  published with both assets `uploaded`:
  `dotfiles_runtime-0.2.3-py3-none-any.whl` (355697 bytes) and
  `dotfiles_runtime-0.2.3.tar.gz` (639246 bytes).
- No product-code change in the release commit beyond version metadata and the
  CHANGELOG entry; the live `~/.local/state/dotfiles/watch-health.json` and
  `~/.config/dotfiles` were not touched, and no other repo/chain leg was
  started.

`Verify:` `git rev-parse v0.2.3 'v0.2.3^{commit}'`;
`gh release view v0.2.3 --json tagName,url,assets --jq
'{tagName,url,assets:[.assets[]|{name,size,state}]}'`;
`gh run view 40428185474 --json conclusion`; `make gates`; `make build`;
`uv run dotfiles-runtime version`.

## Release / pin chain (follow-up, not part of this ticket)

This ticket changes runtime code only. After the implementation is merged and
`make gates` is green on `main`:

1. **Runtime release** — bump `pyproject.toml`, `uv.lock`, and
   `src/runtime/__init__.py` to `0.2.3`; add the `0.2.3` CHANGELOG entry; cut an
   annotated `v0.2.3` tag and attach the sdist/wheel per the repo release
   contract. Current released tag is `v0.2.2`; never move or delete a published
   tag.
2. **dotfiles-provision mirror** — consume `v0.2.3`:
   `ansible/roles/cli_tools/vars/main.yml` runtime `tag: v0.2.3`;
   `ansible/roles/verify/vars/main.yml` `verify_runtime_version: v0.2.3` and
   `verify_runtime_tool_version: 0.2.3`;
   `tests/fixtures/composition/dotfiles/provisioning/cli-tools.yaml` runtime
   `tag: v0.2.3`; update
   `tests/unit/test_verify_role.py::TestVerifyRuntimePinMirror`
   (currently hard-coded to `v0.2.2`). Release the provision engine as the next
   patch (current `v0.1.16`; e.g. `v0.1.17`).
3. **dotfiles-v3 pins** — update `dotfiles/provisioning/cli-tools.yaml` runtime
   `tag: v0.2.3` and `dotfiles/provisioning/installer.pin` to the new provision
   tag (currently `v0.1.16`), then run the v3 pin gates.
4. **Verify each leg** — `git ls-remote --tags … 'refs/tags/v0.2.3*'` and
   `gh release view v0.2.3` for the runtime; `make gates` in
   `dotfiles-provision`; the v3 pin tests after the manifest update.

This chain is deliberately out of scope here: no tag, release, version bump,
CHANGELOG edit, sudo, host change, or other-repo work in this ticket.

## Scope / constraints

- Runtime repo only; no code in this triage session, no live-file change, no
  `~/.config/dotfiles` creation, no daemon restart, no sudo, no host change.
- Do not conflate with the csg stale-container ticket; this is the runtime
  watch-health semantics issue only.
- Keep AD-39/AD-40 intact: the root set stays exactly the five enumerated
  roots, no parent/ancestor watches, no polling, and required-root failures
  remain surfaced.
- Keep the health record backward/forward compatible: additive key, version 1,
  old records readable, unknown keys ignored by older readers.
- No D-Bus/event-contract change and no new dependency.

## Self-contained implementation prompt for the next session

```text
You are implementing BUG-005 in the dotfiles-runtime repo.

Repo:   /home/inumaki/Development/dotfiles/dotfiles-runtime
Ticket: tickets/BUG-005-watch-health-optional-root/ticket.md
Worktree: .worktrees/BUG-005/
Branch: ticket/BUG-005-watch-health-optional-root

Read first: AGENTS.md, tickets/LIFECYCLE.md, this ticket, then
src/runtime/adapters/watch_roots.py, src/runtime/domain/watch.py,
src/runtime/adapters/inotify_watch_source.py,
src/runtime/adapters/watch_health.py,
src/runtime/adapters/daemon_status.py,
src/runtime/application/watch.py,
src/runtime/cli/main.py (inspect daemon / doctor),
src/runtime/adapters/desired_state_reader.py, and
src/runtime/adapters/converge_inputs.py.

Problem: the optional intent document is
$XDG_CONFIG_HOME/dotfiles/desired.json. When its parent directory is absent,
InotifyWatchSource records it as a failed required root ("parent missing"),
WatchStatus.degraded becomes true, and watch-health.json / `inspect daemon`
report the whole watch set as degraded. The reader explicitly treats an absent
intent file as None, so absence is expected, not watch loss.

Implement Option A from the ticket:
1. Add `optional: bool = False` to WatchRoot (last field); mark only the intent
   root optional in enumerate_watch_roots; required roots stay unchanged.
2. Add `optional_absent: tuple[str, ...] = ()` to WatchStatus; keep
   `degraded = bool(self.failed)` so only required roots can degrade.
3. In InotifyWatchSource, track an absent optional root separately from
   `_failed`; log it at INFO once per root, do not call `_record_unwatchable`;
   clear it on successful install, rebuild(), and close(); expose it sorted in
   status(). Required-root warnings/failures must keep the current behavior.
4. Add `optional_absent` to WatchHealthRecord; write/read it as an optional
   list[str] (missing key -> (), wrong shape -> None). Keep version 1 and the
   existing symlink/corruption/atomic-write policy.
5. Add it to DaemonWatchHealthView and assemble_daemon_report; include it in
   `inspect daemon` and `doctor` JSON. In plain output show
   `OPTIONAL (absent, not watched) <path>` as informational; keep
   `degraded`/`UNWATCHED` only for required failures and keep exit 0.
6. In WatchCoordinator, keep the once-per-burst retry condition as
   `status.degraded or status.optional_absent`, so an optional root is watched
   after its parent appears without polling or a restart; unchanged-status
   writes stay suppressed.
7. Add/update the focused tests named in the ticket's "Tests and acceptance":
   optional vs required missing-parent cases, the control case (parent present,
   file absent), health-record legacy/round-trip/invalid-shape cases,
   coordinator retry, inspect JSON/plain output, and doctor. Keep all existing
   exhaustion/IN_IGNORED/healthy tests green.
8. Run the focused tests, then `make gates` on the ticket branch. Merge to main
   only when green, then push. No tag, release, version bump, CHANGELOG edit,
   sudo, host change, live-file edit, or other-repo change; the
   runtime -> provision -> v3 pin chain in the ticket is a separate follow-up.

Acceptance: the deterministic repro prints degraded=False, failed=() and
optional_absent=(intent path,); a missing required root still prints
degraded=True with that root named; inspect/doctor exit 0 and show the optional
root as informational; an old health record without the new key still reads;
make gates is green. The live ~/.local/state/dotfiles/watch-health.json and
~/.config/dotfiles must not be touched.
```
