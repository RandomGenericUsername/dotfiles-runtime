---
id: REQ-017
title: "Doctor supervision checks for the always-on AGS systemd --user units"
kind: requirement
status: in-progress
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-017/
branch: ticket/REQ-017-supervision-doctor
---

# REQ-017 — Doctor supervision checks for the always-on AGS systemd --user units

## Requirement

Provisioning v0.1.37 moved the two always-on AGS apps onto `systemd --user`
units (`dotfiles-ags-bar.service` / instance `ags` / configDir `ags`;
`dotfiles-ags-notifications.service` / instance `notifications` / configDir
`ags-notifications`). The units use `ExecStop=ags quit -i <instance>` and
`Restart=on-failure`; on-demand apps remain launchers. Provisioning owns the
static unit contract and explicitly hands the live leg to the runtime doctor
(`dotfiles-v3/docs/105-appearance-plane-v2.md` §4.3; `dotfiles-provision`
REQ-023 Handoff).

The runtime doctor currently reports appearance/pointer and watch health but
not supervision. It must grow a read-only supervision leg and a narrowly
scoped repair:

1. **Unit state** — `systemctl --user is-active` for both always-on units.
   When systemd or a user session is absent, skip the leg with a clear note
   (never a failure).
2. **Unit PID sanity** — `systemctl --user show -p MainPID --value <unit>`,
   MainPID liveness, and the instance's AGS D-Bus owner PID
   (`busctl --user status io.Astal.<instance>`, cross-checked against
   `ags list`). A live owner outside the unit cgroup is drift; a `gjs` child
   inside the unit cgroup is valid (the `ags run` wrapper is MainPID).
3. **Orphans** — report `ags run` wrappers for an always-on configDir that are
   outside their unit cgroup, plus the `gjs` descendants of those wrappers.
   Never signal them from the check.
4. **Appearance freshness** — report when the ledger says the running app was
   last applied at an older appearance hash than the current generation.
5. **Repair** — `--repair` is limited to the two RFC §4.2 graceful actions:
   `systemctl --user restart <unit>` and `ags quit -i <instance>`. It never
   kills, `pkill`s or process-tree-kills an `ags run` parent, and an absent
   systemd/session is a no-op, not a failure.
6. **Tests** use fake `systemctl`/`ags`/`busctl` seams and cover active,
   inactive, PID mismatch, orphan, absent-systemd skip, stale appearance and
   exactly the allowed repair action set.

## Plan

- `src/runtime/adapters/ags_supervision.py` owns the command/proc seams
  (`systemctl`, `ags list`, `busctl`, `/proc` wrappers, cgroup reader) and the
  two frozen always-on identities. The application layer only composes the
  report and repair.
- `DoctorUseCase` gains an optional supervision probe (default absent, so
  direct/unit construction stays hermetic). `_run_doctor_check` injects the
  real probe.
- `DoctorRepairUseCase` gains an optional supervision repair callable. Pure
  supervision drift is repaired without a reconcile; mixed drift performs the
  safe supervision actions and then the existing reconcile.
- CLI output surfaces the skip note, supervision issues, stale appearance
  notes and the exact repair actions in `--format json`.
- `inspect status` remains the state projection; live supervision stays a
  doctor concern (RFC §4.3 names the doctor as the owner), avoiding a second
  live systemd surface.

## Acceptance

- [ ] `doctor` reports each always-on unit as ok when active, MainPID alive and
      the D-Bus owner inside the unit cgroup; inactive/failed units exit 1.
- [ ] MainPID/D-Bus owner mismatch and orphan `ags run`/`gjs` processes are
      reported as drift without signalling any process.
- [ ] Absent systemd/user session yields a clear skip note and does not change
      doctor's clean exit code.
- [ ] A ledger entry older than the current appearance hash is reported as a
      stale-app note (never fabricated when the ledger is absent).
- [ ] `doctor --repair` activates only `systemctl --user restart <unit>` and
      `ags quit -i <instance>`; a test pins the exact allowed set and the
      absence of `kill`/`pkill`/process-tree actions.
- [ ] Unit tests use fake systemctl/ags/busctl/proc seams and cover active,
      inactive, PID mismatch, orphan, absent-systemd skip and repair actions.
- [ ] README/ARCHITECTURE and CHANGELOG document the leg and the safety rule.
- [ ] `make gates`, `make gates-ci` and `make ci-gates-parity` pass; annotated
      `v0.9.2` lands after a fast-forward merge; no push.

## Evidence / Verify

- RFC: `dotfiles-v3/docs/105-appearance-plane-v2.md:334-353` — doctor unit
  state, MainPID, bus owner, orphans, pointer sanity; repair limited to
  `systemctl --user restart` / `ags quit -i`.
- Handoff: `dotfiles-provision/tickets/REQ-023-appearance-supervision/ticket.md`
  — provisioning owns the static unit contract; live checks are this ticket.
- Unit contract: `dotfiles-provision/ansible/roles/ags_apps_units/templates/dotfiles-ags-app.service.j2`
  — `ExecStart=... ags run -d <spine>/config/<configDir>`, `ExecStop=... ags
  quit -i <instance>`, `Restart=on-failure`.
- Live names: `busctl --user list` on the development host shows
  `io.Astal.ags` and `io.Astal.notifications`; the notification overlay also
  owns `org.freedesktop.Notifications`.
- Verify: `uv run pytest -q tests/unit/test_ags_supervision.py tests/unit/test_doctor_check.py tests/unit/test_doctor_repair.py`
  and `make gates`.
