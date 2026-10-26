---
id: BUG-002
title: "csg adapter timeout: clean up the container and never discard a complete palette"
kind: bug
status: in-progress
lane: 2
severity: medium
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-002/
branch: ticket/BUG-002-csg-adapter-timeout-cleanup
repro: "Build a scratch harness that runs CsgAdapter(timeout=2, templates_dir=...) on the real 8K fixture ~/.local/share/dotfiles/wallpapers/yasuke-dark-7680x4320-21997.jpg (or a fake csg that writes the 8 artifacts then sleeps); observe all 8 artifacts written, PaletteUnavailableError with returncode=None and stderr='', and a leftover csg container."
observed: "Forced 2 s adapter timeout: all 8 palette artifacts were written, the container was left behind (host: 00be5b3bdf2c localhost/csg-pywal-podman:v0.2.0 Exited (137) 8 hours ago confident_hawking), and the adapter reported rc=None, stderr=''. In the production staging path the 8 valid artifacts are deleted by populate_via_staging's exception cleanup, so a valid palette is lost."
expected: "The adapter timeout must exceed csg's internal 300 s timeout (e.g. 330 s) so csg's own cleanup runs; on timeout or host kill the container is removed (including a stale-container sweep for SIGKILL/power loss); every timeout path verifies the 8-artifact set and treats a complete palette as usable or reports a precise failure; no valid palette is discarded; a success-required real-input matrix (8K/4K, warm/cold) and a forced-timeout regression test pass; the 512 MiB container limit is an explicit, evidence-backed decision."
---

# csg adapter timeout: clean up the container and never discard a complete palette

## Triage (`/triage`, 2026-10-26)

- `kind: bug` — a forced timeout loses an already-written valid palette, leaves
  a container behind, and reports an untraceable `rc=None, stderr=''`. That is a
  cleanup/robustness defect in the runtime's csg boundary, not a feature.
- `lane: 2 — openspec-single (one bounded runtime change)` — one contract
  change: "a csg timeout is a bounded, clean failure that cannot destroy a
  complete palette and cannot leak a container." The container-lifecycle half
  may need a coordinated `color-scheme-generator` change; that cross-repo
  dependency is recorded here, not opened as a second implementation ticket.
- `severity: medium` — the default adapter timeout is 60 s and the measured
  real 8K/4K runs are 12.0–14.3 s, so this is not a normal-path failure. When
  it does happen the impact is high (required-palette path loses a valid
  palette and a container leaks), which is why it is not `low`. Evidence:
  handoff measurements and the harness output below.
- `Evidence:` `src/runtime/adapters/csg_adapter.py:100-110` (60 s default),
  `:288-316` (bare `subprocess.run`; `TimeoutExpired` raises without artifact
  verification or cleanup), `:353-383` (artifact verification exists only on
  the success path); `src/runtime/adapters/cache.py:517-622`
  (`populate_via_staging` removes staging on any `populate_fn` exception);
  `src/runtime/cli/main.py:277,612,693,1038,1359,1590,1683` (every production
  `CsgAdapter(...)` uses the default timeout).
- `Verify:` `uv run python -c 'from runtime.adapters.csg_adapter import CsgAdapter; print(CsgAdapter()._timeout)'`
  → `60`.

## Implementation session (2026-10-26)

- Worktree: `.worktrees/BUG-002/`; branch:
  `ticket/BUG-002-csg-adapter-timeout-cleanup` (fast-forwarded to `main`
  `628ce70` before implementation).
- Triage baseline: `CsgAdapter()._timeout` → `60`; timeout path raised
  `PaletteUnavailableError(rc=None, stderr='')` without verifying artifacts;
  `populate_via_staging` then removed the complete 8-artifact set.
- Scope: runtime half only (timeout ordering, graceful child termination,
  complete-artifact verification, precise timeout errors, regression tests,
  success-required real-input matrix, 512 MiB decision). The historical
  null-palette investigation stays closed.

## Closed context — do not reopen

The historical `pal=NULL` rows are **closed**. They were caused by the old
0.1.0 visible-first pipeline saving `palette=None` before derivation, then the
session being interrupted (WEG/magick OOM or logout), and reconciled every
~95 s. v0.2.0 is deployed and verified; both real fixtures generate palettes
successfully. This ticket is **only** the remaining forced-timeout cleanup
defect. Do not re-investigate the null-palette history here.

## Repro

Cheap, host-safe adapter proof (no container, no live state; the exact command
used for the evidence below):

```bash
# /tmp/opencode/fake-slow-csg writes all 8 artifacts into
# $COLORSCHEME__OUTPUT__DIRECTORY, logs the names, then sleeps 30 s.
chmod +x /tmp/opencode/fake-slow-csg
mkdir -p /tmp/opencode/bug002/templates
printf 'x\n' > /tmp/opencode/bug002/templates/colors.yaml.j2

cd /home/inumaki/Development/dotfiles/dotfiles-runtime
uv run python - <<'PY'
from pathlib import Path
from runtime.adapters.csg_adapter import CsgAdapter
from runtime.adapters.hashing import canonical_hash_dir, hash_file, palette_entry_hash
from runtime.domain.models import PaletteUnavailableError

wall = Path.home()/".local/share/dotfiles/wallpapers/yasuke-dark-7680x4320-21997.jpg"
tpl = Path("/tmp/opencode/bug002/templates")
out = Path("/tmp/opencode/bug002/out")
ph = palette_entry_hash(hash_file(wall), canonical_hash_dir(tpl))
out = out / ph
try:
    CsgAdapter(timeout=2, csg_bin="/tmp/opencode/fake-slow-csg", templates_dir=tpl).generate(wall, out)
except PaletteUnavailableError as exc:
    print("rc=", exc.returncode, "stderr=", repr(exc.stderr))
print("artifacts=", sorted(p.name for p in out.iterdir()))
PY
```

Production-path proof of the loss (same fake csg, real `DerivationPipeline`
with a scratch state root and an install-spine symlink to the templates dir)
shows the fake wrote all 8 files, then:

```
pipeline raised PaletteUnavailableError: csg generate timed out after 2s
staging_dirs_after= []
palette_dirs_after= []
fake_csg_wrote= ['colors.adw.css', 'colors.btop.theme', 'colors.conf',
 'colors.gtk.css', 'colors.kitty', 'colors.rasi', 'colors.sequences', 'colors.yaml']
```

Real-container repro (to run in the implementation session, **opt-in**, with a
cleanup trap and a scratch `XDG_STATE_HOME`; never the live state root):

```bash
# 1. Record the pre-existing container set (read-only).
podman ps -a --format '{{.ID}} {{.Image}} {{.Status}} {{.Names}}' > /tmp/opencode/bug002/containers-before.txt
# 2. Run the real 8K fixture through the adapter with timeout=2 in a scratch root
#    (same harness as tests/integration/test_required_palette_real_inputs.py,
#    but CsgAdapter(timeout=2)).
# 3. Observe: 8 artifacts in the scratch palette dir; rc=None; stderr='';
#    one NEW localhost/csg-pywal-podman container left behind.
# 4. Clean up only the container created by step 2 (match pre/post diff).
```

## Observed

- Forced 2 s timeout, fake-csg adapter harness: raised
  `PaletteUnavailableError` after 2.01 s with `returncode=None`,
  `stderr=''`, message `csg generate timed out after 2s`; **all 8 artifacts**
  were present (`colors.yaml`, `colors.conf`, `colors.gtk.css`,
  `colors.adw.css`, `colors.sequences`, `colors.rasi`, `colors.kitty`,
  `colors.btop.theme`).
- Production staging path: `populate_via_staging` deletes the staging dir on
  the exception, so the 8 complete artifacts disappear and no palette entry is
  published (`staging_dirs_after=[]`, `palette_dirs_after=[]`).
- Host leftover from the original forced-timeout investigation:
  `00be5b3bdf2c localhost/csg-pywal-podman:v0.2.0 Exited (137) 8 hours ago confident_hawking`,
  created `2026-10-26 06:24:47 -0500`, `cmd=["sleep","infinity"]`, labels only
  `{"io.buildah.version":"1.45.1"}` (no csg identifier). It is stopped but not
  removed — a leaked container object, not a running workload.
- Measured csg timings (handoff, 2026-10-26): warm 8K 13.1–13.3 s, 4K
  12.0–12.2 s; under load 14.3 / 13.1 s. Adapter 300 s behaves the same as
  60 s because csg's internal timeout is also 300 s. Peak container memory
  310 MiB (8K), 190 MiB (4K); 512 MiB passes, 256 MiB fails 8K; no csg OOM.
- `csg dump-config` on this host: `container.timeout_seconds = 300`,
  `container.memory_limit = "512m"`; csg version `0.2.0 contract 0.2.0`.

## Expected

1. **Timeout ordering:** the adapter timeout is above csg's internal 300 s
   (recommended 330 s) so csg's own `exec_container` timeout fires first and
   its `finally` removes the container. A 300 s adapter timeout is not enough
   (same race as 60 s).
2. **Container cleanup:** on adapter timeout, no new container remains —
   running or stopped. Because the adapter only knows the `csg` subprocess, not
   the container id, this is primarily a csg-owned lifecycle fix; host kill
   (`SIGKILL`, power loss) additionally needs a stale-container sweep (label or
   image/age based). Record which layer owns each case.
3. **No lost valid palette:** after the child is terminated and the container
   is stopped, verify the complete 8-artifact set. If complete (and its hashes
   are stable), return a normal `PaletteEntry` and let staging publish it; if
   incomplete, keep the hard `PaletteUnavailableError` with `rc`/stderr and a
   precise "incomplete csg output" reason. Never let `populate_via_staging`
   silently delete a complete set.
4. **Traceability:** a timeout failure records rc (or an explicit "killed by
   timeout" marker), stderr, elapsed time, fixture hash, and whether the
   artifact set was complete. `rc=None, stderr=''` alone is not acceptable.
5. **Success-required real-input matrix:** 8K and 4K, warm and cold, must be
   required successes (not "palette or clean error"); the current integration
   test `tests/integration/test_required_palette_real_inputs.py` accepts a
   hard error and must be tightened for the success rows.
6. **Forced-timeout regression test:** a deterministic test (fake slow csg in
   unit tests; real container opt-in) pins: timeout > internal, cleanup
   attempted, complete artifacts accepted, incomplete artifacts rejected, no
   staging leftover.
7. **512 MiB decision:** record an explicit decision, not an assumption. The
   current evidence (310 MiB peak at 8K on a 512m limit, 256m fails) supports
   **keeping 512m** unless the matrix shows less than ~25% headroom. The limit
   is csg-owned (`color-scheme-generator/src/color_scheme_generator/domain/models.py:131-137`,
   `src/color_scheme_generator/defaults/settings.toml:19-24`); if it is raised,
   the change belongs to csg (or an explicit `COLORSCHEME__CONTAINER__MEMORY_LIMIT`
   env override from the runtime), never to an undocumented runtime constant.

## Evidence

| Claim | Source / command | Result |
| --- | --- | --- |
| Adapter default is 60 s and every production construction uses it | `csg_adapter.py:100-110`; `main.py:277,612,693,1038,1359,1590,1683` | `CsgAdapter()._timeout` → `60`; no caller passes `timeout=` |
| Timeout path does not verify artifacts or clean up | `csg_adapter.py:288-316`; `:353-383` | `TimeoutExpired` → `PaletteUnavailableError(returncode=None, stderr=...)`; verification only after `returncode == 0` |
| Staging cleanup deletes a complete set | `cache.py:517-622` | `except BaseException: shutil.rmtree(staging, ...); raise` |
| Forced 2 s timeout writes 8 artifacts, rc=None, stderr='' | fake-csg harness above | `raised=PaletteUnavailableError after 2.01s`, `returncode=None stderr=''`, `artifact_count=8` |
| The 8 artifacts are then lost | pipeline harness above | `staging_dirs_after=[]`, `palette_dirs_after=[]`, fake wrote all 8 |
| csg internal timeout/memory are 300 s / 512m | `color-scheme-generator/src/color_scheme_generator/domain/models.py:131-137`; `.../defaults/settings.toml:19-24`; `csg dump-config` | `timeout_seconds = 300`, `memory_limit = "512m"` |
| csg cleans containers only in a Python `finally` | `color-scheme-generator/src/color_scheme_generator/adapters/oci_container_runtime.py:44-70` | `detach=True, remove=False`; `finally: remove(container_id, force=True)`; SIGKILL bypasses `finally` |
| A leftover container exists on the host | `podman ps -a` / `podman inspect 00be5b3bdf2c` | `Exited (137) ... confident_hawking`, `sleep infinity`, no csg label |
| Timings and memory facts | `/home/inumaki/.opencode/plan/13-HANDOFF-csg-adapter-timeout-cleanup.md:31-35` | 13.1–13.3 s / 12.0–12.2 s; 310/190 MiB; 512 passes, 256 fails |

## Hypotheses

1. **Primary (code-backed):** the adapter kills the host `csg` process at 60 s.
   csg's container is created `detach=True` and removed only in a Python
   `finally` inside `OciContainerRuntimeAdapter.run`; a killed process never
   reaches it. Raising the adapter timeout above csg's 300 s lets csg's own
   timeout path run the `finally` and remove the container.
2. **Artifact loss is runtime-local:** the adapter verifies artifacts only on
   success, so a timeout always raises; the caller's staging cleanup then
   deletes whatever csg already wrote. Even a complete, valid 8-artifact set is
   discarded. This half can be fixed in `dotfiles-runtime` without csg changes.
3. **Host kill needs a reaper:** no in-process `finally` can run on SIGKILL or
   power loss. A stale-container sweep is required (prefer a csg-owned label
   such as `csg.session=<uuid>` plus `csg` startup/reconcile cleanup; a
   runtime-side `podman rm` would violate the "runtime only invokes CLIs"
   charter). The existing leftover has no identifying label, so today a sweep
   can only use image + age and is unsafe to automate blindly.
4. **Memory is not the cause:** no OOM was observed; 512m passes the measured
   8K peak of 310 MiB. The 256m failure is a limit experiment, not evidence for
   changing production.

## Investigation / acceptance plan (next session)

1. **Reproduce red (30 min):** run the fake-csg harness and the pipeline
   harness above; capture outputs under `/tmp/opencode/bug002-*.txt`. Then run
   the opt-in real 8K container repro in a scratch `XDG_STATE_HOME`, capturing
   the pre/post `podman ps -a` diff and cleaning up only the new container.
2. **Confirm the csg lifecycle (30 min):** trace `csg generate` → 
   `container_processor` → `OciContainerRuntimeAdapter.run` →
   `oci_runtime.containers.exec_container(timeout=300)`. Record whether the
   host `csg` process handles SIGTERM (it does not install a handler today) and
   whether `--rm`/`remove` is used (it is `remove=False` + explicit remove).
3. **Decide the ownership split (30 min):** runtime-only fix = timeout > 300,
   complete-artifact verification, precise error fields, regression tests.
   csg-side fix = signal-safe cleanup and stale-container reaper/label. Record
   the cross-repo handoff; do not open a separate implementation ticket in this
   triage turn.
4. **Implement the runtime half (2 h):**
   - raise the default `CsgAdapter` timeout to 330 s (or a named constant above
     csg's 300 s) and document the coupling;
   - extract one 8-artifact verifier used by both success and timeout paths
     (single source of truth with `PALETTE_ARTIFACT_NAMES`; keep layering
     clean);
   - on `TimeoutExpired`, after the child is dead, verify the set; return
     `PaletteEntry` when complete, otherwise raise
     `PaletteUnavailableError` with an explicit timeout marker, elapsed time,
     completeness detail, `rc=None` allowed only when justified;
   - never rely on `populate_via_staging` to preserve evidence: if incomplete,
     include the artifact names found/missing in the error before cleanup.
5. **Tests (2 h):**
   - unit: timeout + complete artifacts → entry; timeout + missing artifact →
     typed error naming the missing file; timeout + partial write → no staging
     leftover; default timeout > 300 assertion;
   - integration (opt-in): real 8K/4K warm/cold success matrix, required;
   - regression: `podman ps -a` pre/post shows no new container after a
     timeout.
6. **512 MiB decision (30 min):** record keep-vs-raise with the measured peaks
   and the csg ownership. If keep, add a one-line rationale to the ticket and
   do not touch csg settings. If raise, file the csg-side handoff separately.
7. **Gates / release (as usual):** `make test`, `make gates`,
   `make test-integration`, real-input matrix, then merge; no hotfix — this is
   a separate runtime release after the current train.

## Acceptance

- [ ] Default/effective adapter timeout is above csg's 300 s internal timeout
      and the coupling is documented in code and the ticket.
- [ ] A forced 2 s timeout on the real 8K fixture leaves no new container
      (running or stopped); a pre/post `podman ps -a` diff is empty.
- [ ] A forced timeout that produced all 8 artifacts yields a usable
      `PaletteEntry` (or a precise, traceable failure that preserves the
      artifacts for diagnosis) — no silent loss.
- [ ] A forced timeout with a missing artifact fails with a typed error naming
      the artifact set and does not publish a partial entry.
- [ ] Real-input matrix 8K/4K × warm/cold is success-required and green.
- [ ] The 512 MiB decision is recorded with evidence and an owner.
- [ ] `make gates` green on the merged ticket branch.

## Scope / constraints

- No production code or default change in this triage step; no host changes, no
  sudo, no provisioning.
- The deployed v0.2.0 hard-error contract stays: this is cleanup/robustness,
  not a return to visible-first or a silent degradation.
- Historical null-palette rows stay closed; do not reopen them.
- Container lifecycle is csg-owned per `AGENTS.md` §2; any runtime change must
  stay inside the "invoke the CLI, never run containers" boundary.
- The live state root is not a test fixture; use `tmp_path` / a scratch
  `XDG_STATE_HOME`.

## Next-session starting points

```bash
cd /home/inumaki/Development/dotfiles/dotfiles-runtime
git worktree list                      # .worktrees/BUG-002/ on ticket/BUG-002-csg-adapter-timeout-cleanup
git switch ticket/BUG-002-csg-adapter-timeout-cleanup
sed -n '100,120p;288,320p;353,395p' src/runtime/adapters/csg_adapter.py
sed -n '510,625p' src/runtime/adapters/cache.py
sed -n '690,760p' src/runtime/application/derive.py
sed -n '95,128p;315,345p' tests/unit/test_csg_adapter.py
DOTFILES_REAL_WALLPAPERS=$HOME/.local/share/dotfiles/wallpapers \
  uv run pytest -q -s tests/integration/test_required_palette_real_inputs.py
podman ps -a --filter ancestor=localhost/csg-pywal-podman:v0.2.0 \
  --format '{{.ID}} {{.Status}} {{.Names}}'
```

Read the two handoffs
(`/home/inumaki/.opencode/plan/13-HANDOFF-csg-adapter-timeout-cleanup.md` and
this ticket) before touching code. Treat the existing
`00be5b3bdf2c` container as evidence; do not remove it without recording the
pre/post state.
