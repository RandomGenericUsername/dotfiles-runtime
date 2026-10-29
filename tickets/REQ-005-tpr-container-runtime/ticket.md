---
id: REQ-005
title: "TprAdapter uses the container runtime; no host tpr-native dependency"
kind: requirement
status: in-progress
lane: 2
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-004/
branch: ticket/REQ-004-runtime-palette-fixes
---

# REQ-005 — TprAdapter container runtime

## Why

The adapter currently hard-codes `THEME__RUNTIME__MODE=local`; the native
engine is a host binary. Provisioning builds `tpr-native-<engine>:v0.4.0` but
does not install `tpr-native`, so a fresh host is green and the first tpr set
fails `EngineUnavailableError`. The proper fix is to use the container runtime
that already ships both `tpr` and `tpr-native`, matching the provisioned tpr
settings `[runtime].mode = "container"` and the containerized-engine
requirement.

## Requirement

- `TprAdapter` no longer forces local mode. It resolves the tpr runtime from the
  producer config (`tpr_runtime`, default `container` for tpr) or the tpr
  settings, and sets `THEME__RUNTIME__MODE` accordingly.
- It pins the spine templates directory explicitly (via the supported
  `THEME__TEMPLATES__DIRECTORY` env or the tpr `--templates-dir` global flag),
  so CWD discovery cannot select an unrelated `templates/` directory. The
  templates hash it uses for the cache key must match the mounted catalog.
- Engine remains `native`; the container image tag/engine come from the
  provisioned tpr settings; no host `tpr-native` is required.
- Local mode remains available for tests/dev via an explicit flag/config, never
  by accidental CWD or stale env.
- Container mode is compatible with the tpr invocation contract; if the
  templates pin requires a new argv token, bump the contracts pin deliberately;
  an allowlisted env key is preferred.

## Acceptance

- [ ] A fresh host with no `tpr-native` on PATH generates a tpr/native palette
  by container and writes the same 8 artifacts/cache key as the verified local
  tpr output.
- [ ] The adapter pins the spine templates dir; a selector-CWD decoy
  `templates/ags-app` cannot influence tpr generation.
- [ ] `tpr_native` is not installed/required by provisioning; manual copy is
  removed from the acceptance path.
- [ ] Tests cover container env, templates pin, engine selection, and a real
  container-target generate; `make gates` green; release tag pushed.
- [ ] `dotfiles-provision` verify proves the selected tpr path can generate and
  pins this runtime release.

## Cross-references

- `dotfiles-provision` BUG-007 — provisioning native-helper removal, verify and
  runtime pin.
- `dotfiles-runtime` REQ-004 — producer config in the same release.
- `theme-producer` v0.4.0 already ships the container/native image.
