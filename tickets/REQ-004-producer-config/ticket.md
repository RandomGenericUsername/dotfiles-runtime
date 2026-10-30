---
id: REQ-004
title: "Runtime-owned palette producer config (no ambient-env authority)"
kind: requirement
status: done
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-004/
branch: ticket/REQ-004-runtime-palette-fixes
---

# REQ-004 — Runtime-owned palette producer config

## Why

`RUNTIME__PALETTE__PRODUCER` is read from ambient env at every composition root.
A selector/Hyprland process that predates a producer change keeps `csg` forever
(the live selector PID has `csg` while the daemon/environment.d say `tpr`), and
the provisioning seed used the single-underscore `RUNTIME_PALETTE_PRODUCER`
which the runtime ignores. The producer must be a runtime-owned, provisioning-
rendered value, not caller environment.

## Requirement

Add a versioned config file at:

```text
<install_spine>/config/dotfiles-runtime/producer.json
{"schema_version": 1, "producer": "tpr", "tpr_engine": "native", "tpr_runtime": "container"}
```

Resolution order in `dotfiles-runtime`:

1. explicit `--producer csg|tpr` CLI flag (one-shot override);
2. the config file;
3. default `csg` with a warning when the file is absent.

`RUNTIME__PALETTE__PRODUCER` / `RUNTIME__PALETTE__TPR_ENGINE` are **not
authoritative**; if present and different, warn and report `ignored_env`.
Invalid/malformed config fails loud. Every CLI and daemon composition root uses
the same resolver at operation time.

Surface the resolved value:

- `dotfiles-runtime inspect producer --format json`:
  `producer`, `source`, `engine`, `runtime`, `config_path`, `ambient_env`,
  `ignored_env`, `warnings`.
- `wallpaper set` JSON includes `producer`, `producer_source`, `producer_engine`.
- Palette failures set `tool` to the real producer (`tpr` must not be recorded
  as `csg`).

## Acceptance

- [ ] Config `tpr` wins over ambient `csg`; a selector launched before
  environment.d still applies tpr.
- [ ] No env is authoritative; a missing config defaults to csg with a warning;
  malformed/unknown config fails loud.
- [ ] `inspect producer` and `wallpaper set` report the resolved source/engine;
  stale env is visible, not silent.
- [ ] `failures.jsonl` records `tool: tpr` for tpr failures.
- [ ] Unit/integration tests cover config > env, default warning, malformed
  config, stale-env selector-shaped subprocess, daemon converge and CLI apply.
- [ ] `make gates` green; release tag `v0.4.1` (or next) pushed.

## Cross-references

- `dotfiles-provision` BUG-007 — render the config, remove env.d/unit producer
  authority, verify the file.
- `ags-wallpaper-selector` BUG-001 — selector apply path unchanged; optional
  startup diagnostic can use `inspect producer`.
- `dotfiles-runtime` REQ-005 — tpr container runtime in the same release.
- `dotfiles-runtime` BUG-007 — palette diagnostics in the same release.

## Closeout evidence (2026-10-30)

- Released `v0.4.1` (tag pushed); gates: 2299 passed, lint/contracts/hermetic/lock/docs green.
- Live `producer.json` is `tpr/native/container`; `inspect producer` with ambient
  `RUNTIME__PALETTE__PRODUCER=csg` reports `source: config`, `ignored_env: csg`.
- Selector-shaped set reports `producer=tpr`, `producer_source=config`,
  `producer_engine=native`; daemon/unit no longer carry producer env.
