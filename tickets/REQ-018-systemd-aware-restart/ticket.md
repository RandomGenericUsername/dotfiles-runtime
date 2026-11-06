---
id: REQ-018
title: "Systemd-aware AGS restart lane and the v2 pointer cutover"
kind: requirement
status: done
lane: 3
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-018/
branch: ticket/REQ-018-systemd-aware-restart
---

# REQ-018 — Systemd-aware AGS restart lane and the v2 pointer cutover

## Requirement

The v2 acceptance test exposed a live split-brain: after a skin switch the
manifest-driven AGS restart lane quit the bar and relaunched it with a raw
`ags run`, even though provisioning v0.1.37 owns that app through
`dotfiles-ags-bar.service`. The result was an orphan bar outside the unit,
`dotfiles-ags-bar.service` failed/inactive, and `doctor` reporting
`supervision.units[bar] active=false, main_pid=0, bus_owner_pid=<orphan>,
bus_owner_in_unit=false, detail="unit inactive"`. Notifications stayed hot.

The restart lane must become systemd-aware, and the completed v2 appearance
cutover (REQ-015) must remove the remaining v1 skin pointer surface.

1. **Unit discovery (no app list).** Discover `systemctl --user` units matching
   `dotfiles-ags-*.service`, read each unit's `ExecStart`, and derive the
   configDir from its `ags run -d <spine>/config/<configDir>` target. A unit
   is "unit-managed" when its target resolves to a consumed deployed configDir.
2. **Unit-managed restart.** Restart the unit with
   `systemctl --user restart <unit>`, serialized by the existing refresh
   operation lock, and let systemd own the quit/start. Verify the instance
   name returns through the AGS instance oracle. Never raw-`ags quit` /
   raw-`ags run` a unit-managed app. Non-unit apps keep the existing
   quit + release-wait + launch path unchanged.
3. **No double restart.** One refresh restarts each discovered unit at most
   once; the bar is not also treated as a raw tool after a unit restart.
4. **Failure policy.** A failed `systemctl --user restart` (or a unit-managed
   instance that never returns) is surfaced in the refresh record/ledger and
   therefore in `doctor`; there is no raw-launch fallback for a unit-managed
   app.
5. **v1 cutover (folds REQ-015).** Stop publishing every v1 skin pointer
   (`current/skins/`, `current/skin.json`, `config/<configDir>/skin.css`,
   `config/<configDir>/skin.json`). Prune existing runtime-owned
   `config/<configDir>/skin.{css,json}` links; never remove appearance
   pointers, palette pointers, user files or user symlinks. `doctor` reports
   no v1/v2 skew on a clean v2 host and treats a reappearing runtime-owned v1
   pointer as real drift.
6. **Tests** use fake `systemctl`/units: ExecStart parsing and activation,
   unit-managed restart uses only `systemctl --user restart` (no `ags quit` /
   `ags run`), non-unit fallback unchanged, restart failure surfaces, v1
   pruning is safe, and doctor is supervision-clean with no skew item after a
   unit restart.

## Acceptance

- [x] The restart lane discovers units from `systemctl --user` + `ExecStart`
      and never hardcodes AGS app names.
- [x] A unit-managed app restarts only through `systemctl --user restart
      <unit>`; the record argv is that exact command and `ags quit`/`ags run`
      are not invoked.
- [x] A non-unit restart app still uses the quit + release + launch path.
- [x] A failed or unconfirmed unit restart surfaces in the refresh
      record/ledger and never falls back to a raw launch.
- [x] One refresh performs at most one restart per unit.
- [x] No runtime path publishes a v1 skin pointer; stale runtime-owned
      `config/<configDir>/skin.{css,json}` links are pruned while appearance
      pointers and user files/links are untouched.
- [x] `doctor` reports no v1/v2 skew after the cutover.
- [x] Docs/CHANGELOG record the systemd-aware restart and the final v1
      removal.
- [x] `make gates` and `make gates-ci` pass; annotated `v0.9.3` lands after a
      fast-forward merge and is pushed with main.

## Evidence / Verify

- Live failure: `dotfiles-runtime doctor --format json` →
  `supervision.units[bar] active=false, main_pid=0,
  bus_owner_pid=<orphan>, bus_owner_in_unit=false, detail="unit inactive"`
  after a skin switch; notifications kept its PID and repainted.
- Unit contract (sibling checkout):
  `dotfiles-provision-v0.1.37/ansible/roles/ags_apps_units/templates/dotfiles-ags-app.service.j2`
  — `ExecStart=… ags-run-locked --exec … -- ags run -d
  <spine>/config/<configDir>`, `ExecStop=ags quit -i <instance>`.
- Unit names on the host: `dotfiles-ags-bar.service` (instance `ags`,
  configDir `ags`), `dotfiles-ags-notifications.service` (instance
  `notifications`, configDir `ags-notifications`).
- **Exact unit-managed restart command (the only restart command issued):**
  `systemctl --user restart dotfiles-ags-bar.service` for the bar (and
  `systemctl --user restart dotfiles-ags-notifications.service` when that
  unit is the discovered target). The command is derived from
  `systemctl --user list-unit-files 'dotfiles-ags-*.service'` + each
  `ExecStart`'s `ags run -d <spine>/config/<configDir>`; no app name is
  hardcoded. It is serialized by the refresh operation lock and followed by an
  `ags list` instance-name verification; raw `ags quit`/`ags run` are not
  issued for a unit-managed app.
- Implementation commit `3648977` adds
  `adapters/ags_systemd_units.py`, the systemd branch in
  `AgsReloader.restart_deployed_app`/`reload`, the failure-carrying refresh
  result, the v1 pruning and the doctor cutover behavior.
- Verify: `uv run pytest -q tests/unit/test_ags_systemd_units.py
  tests/unit/test_ags_reloader.py tests/unit/test_ags_app_refresh.py
  tests/unit/test_skins.py tests/unit/test_appearance.py
  tests/unit/test_doctor_check.py` → `164 passed`; full fast suite →
  `2343 passed, 3 skipped, 10 deselected`.
- Gates: `make gates` → rc=0 (`test` 2343 passed, 3 skipped, 10 deselected;
  `lint` All checks passed; `contracts-check` 54 passed, 1 skipped; `hermetic`
  2343 passed, 3 skipped, 10 deselected; `lock-check` resolved 25 packages;
  `docs-check` rc=0). `make gates-ci` → rc=0 with the same counts.
  `make ci-gates-parity` → `PASS` (gates=6, jobs=8, exempt
  `ci-gates-parity, release`).
- No `apply` was run. The v0.9.3 tag and fast-forward merge/push are recorded
  in `INDEX.md` after release.
