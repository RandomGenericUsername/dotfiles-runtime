---
id: BUG-009
title: "v0.6.0 release metadata still reports 0.5.2"
kind: bug
status: done
lane: direct
severity: high
links:
  openspec: none
  bmad: none
worktree: .worktrees/BUG-009/
branch: ticket/BUG-009-release-version-metadata
repro: "git checkout v0.6.0 && grep '^version' pyproject.toml && uv run dotfiles-runtime version"
observed: "pyproject.toml says 0.5.2; `dotfiles-runtime version` prints 0.5.2; provisioning verify fails: expected the checkout/pin/`version` to agree on v0.6.0"
expected: "The installed metadata reports the tag: `dotfiles-runtime version` prints 0.6.1 for the v0.6.1 tag"
---

# v0.6.0 release metadata still reports 0.5.2

## Repro

```sh
git checkout v0.6.0
grep -n '^version' pyproject.toml
grep -n '__version__' src/runtime/__init__.py
uv run dotfiles-runtime version
```

## Observed

```
pyproject.toml:3:version = "0.5.2"
src/runtime/__init__.py:5:__version__ = "0.5.2"
0.5.2
```

`dotfiles-provision apply` reaches:

```
verify : Assert the dotfiles-runtime checkout is the pinned tag and reports the pinned version
Expected ... `dotfiles-runtime version` printing 0.6.0. Observed: stdout=0.5.2, pin=v0.6.0.
```

The v0.6.0 tag commit only changed `CHANGELOG.md`; the version bump was
missed.

## Expected

The installed version metadata agrees with the tag. Because v0.6.0 is an
immutable published tag, the fix is a new patch release, `v0.6.1`.

## Fix notes

- Root cause: the v0.6.0 release commit updated `CHANGELOG.md` but not
  `pyproject.toml`, `uv.lock` or `runtime.__version__`.
- Fix: bump all three to `0.6.1`; no code behaviour changes.
- Release: annotated tag `v0.6.1`; the composition pin moves to `v0.6.1`.
- Regression check: `uv run dotfiles-runtime version` prints `0.6.1` and
  `dotfiles-provision apply` verify passes.

## Evidence

- `make gates` — see the gate log recorded in the release handoff.
- `uv lock --check` — resolved 25 packages, no changes.
- `uv run dotfiles-runtime version` → `0.6.1`.
- `uv run dotfiles-runtime monitors list --format json` → unchanged
  `schema_version: 1` payload.
