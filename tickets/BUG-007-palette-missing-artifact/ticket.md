---
id: BUG-007
title: "Palette step intermittently fails with missing colors.yaml under the AGS selector"
kind: bug
status: proposed
lane: 2
severity: high
links:
  openspec: none
  bmad: none
worktree: none
branch: none
repro: "Apply a wallpaper from the AGS wallpaper selector after the tpr cutover while the selector still has RUNTIME__PALETTE__PRODUCER=csg. The runtime set fails at the palette stage with `csg did not write expected artifact: .../colors.yaml`; the wallpaper is already on screen and effects/icons/consumers are not updated. Direct csg and a sequential runtime csg set for the same image succeed."
observed: "2026-10-29 ~/.local/state/dotfiles/failures.jsonl: set-failed white_tailed_eagle.png at 22:53:24Z and 22:55:27Z, shani.png at 22:59:00Z, stage palette, tool csg, returncode null, `csg did not write expected artifact: .../colors.yaml`. The same white_tailed_eagle run succeeded sequentially at ~23:04Z with palette e2f7b58…, the exact entry hash from the failed staging path. The selector process had RUNTIME__PALETTE__PRODUCER=csg while the daemon had tpr."
expected: "A retry or a second wallpaper set must not leave the runtime in a partial state; failures must carry the producer's stdout/stderr and exit code, and concurrent/repeated applies must be serialized or rejected with a clear busy error before the visible palette step."
---

# BUG-007 — Palette step intermittently fails with missing colors.yaml

## Summary

The user reported that switching wallpapers from the AGS selector now always
stops at “Color palette generated” with an error, after several wallpapers
failed. The runtime's failure log shows CSG producing no `colors.yaml` even
though the command returned success. The same image succeeds when the exact
same runtime command is run sequentially from the shell, and direct
`csg generate` succeeds. That points at a runtime/selector interaction
(concurrency, interrupted staging cleanup, or a stale producer), not at the
image format or the CSG extractor itself.

## Evidence

Failure log (`~/.local/state/dotfiles/failures.jsonl`):

```json
{"ts":"2026-10-29T22:53:24.246725Z","trigger":"set-failed",
 "wallpaper_path":".../white_tailed_eagle.png",
 "wallpaper_hash":"ab5c0891…",
 "stage":"palette","tool":"csg","returncode":null,
 "stderr":"csg did not write expected artifact:
   .../.staging-764946-.../e2f7b58f…/colors.yaml",
 "error":"palette apply failed: csg did not write expected artifact: ..."}
```

Two more entries for the same image and for `shani.png`:

- `2026-10-29T22:55:27.733149Z` — white_tailed_eagle, same missing path
- `2026-10-29T22:59:00.083138Z` — shani.png, different staging/hash

Successful control:

```text
$ RUNTIME__PALETTE__PRODUCER=csg dotfiles-runtime wallpaper set \
    .../white_tailed_eagle.png --format json
{"palette":"e2f7b58fa9b55c12fd1ae5902f41e1e52f68f72be2ed011af096edbb31003eeb",
 "cache_hits":{"palette":false,"effects":false,"icons":false}}
```

`e2f7b58f…` is exactly the hash subdirectory named in the failed staging path.
The image is a 1920×1080 8-bit RGBA PNG; direct `csg generate` and the
sequential runtime path both produce the 8 artifacts.

Selector context (see `ags-wallpaper-selector` BUG-001):

```text
/proc/716589/environ: RUNTIME__PALETTE__PRODUCER=csg
daemon:              RUNTIME__PALETTE__PRODUCER=tpr
```

The selector's `execAsync(["dotfiles-runtime","wallpaper","set",image])`
inherits csg, so the failed/passing paths are both CSG; the tpr cutover itself
is not what fails.

## Triage hypotheses

1. **Overlapping applies / staging cleanup.** The selector can spawn a new set
   while a previous set is still in flight. If the seed mutex does not cover the
   whole visible+themed set, or if staging cleanup removes a directory another
   run is using, CSG can return 0 with no artifact. Verify the lock scope and
   staging ownership; add a two-concurrent-set repro.
2. **Selector lifecycle.** The selector may kill or reload its child when the
   window closes/apply state changes; a killed `dotfiles-runtime` can leave a
   partial staging tree and a follow-up recovery pass. Check whether the failed
   entries correlate with selector restarts/clicks.
3. **CSG container race.** CSG runs in a container; concurrent `csg` processes
   may collide on a shared container name/temp path and return 0 without the
   host output. If this is reproducible with two direct `csg generate` runs,
   open a CSG ticket and link it here.
4. **Diagnostics gap.** The CSG adapter's step 9 raises `RuntimeError` with only
   the missing path; it drops `result.returncode`, stdout and stderr. The
   failure record therefore cannot distinguish CSG crash, container collision,
   or cleanup. Capture and persist them before raising.

## Immediate workaround

- Restart the selector (or log out/in) so it inherits the daemon's tpr producer;
  direct tpr/native sets work. The durable fixes are the selector producer
  ticket and this runtime triage.

## Done-when

- [ ] A repeated/overlapping selector apply either succeeds or fails with a
  clear busy/lock error; it never leaves “palette generated” with a missing
  artifact.
- [ ] A failed palette entry records the producer's stdout/stderr and actual
  return code (and termination/timeout information) in `failures.jsonl`.
- [ ] A deterministic repro exists for the concurrency/staging hypothesis, or
  the issue is shown to be CSG-internal and linked to a CSG bug.
- [ ] The fix is covered by a regression test in the runtime; a CSG change, if
  needed, carries its own ticket.

## Cross-references

- `ags-wallpaper-selector` BUG-001 — stale `RUNTIME__PALETTE__PRODUCER=csg`.
- `dotfiles-provision` BUG-006 — rofi consumer link absent after bootstrap.
- `dotfiles-provision` BUG-007 — tpr native helper not provisioned.
- `color-scheme-generator` is the upstream candidate if CSG is proven to exit 0
  without writing an artifact under concurrency.
