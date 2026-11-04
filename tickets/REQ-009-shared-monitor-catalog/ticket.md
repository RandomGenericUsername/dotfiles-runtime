---
id: REQ-009
title: "Shared monitor catalog service"
kind: requirement
status: done
lane: 3
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-009/
branch: ticket/REQ-009-shared-monitor-catalog
---

# Shared monitor catalog service

## Requirement

Add a read-only, language-agnostic monitor catalog to `dotfiles-runtime`:

```sh
dotfiles-runtime monitors list --format json
```

It returns the connected, enabled, non-mirrored Hyprland outputs with stable
connector ids, labels, geometry, scale, transform, focus and mirror metadata.
The runtime's wallpaper code and the capture tool consume the same descriptors.

## Acceptance

- [x] `dotfiles-runtime monitors list --format json` emits `schema_version: 1`,
  `source`, and `monitors[]` with the contract in the capture EPIC-002
  `design.md` §2.2.
- [x] Disabled and mirrored outputs are filtered; order is x, y, connector.
- [x] Headless/unavailable returns exit 0 with `source: "unavailable"` and
  `monitors: []`.
- [x] `IMonitorSource.detect_monitors()` behavior and all existing runtime tests
  are unchanged.
- [x] A runtime-owned schema at
  `contracts/schemas/monitors-list.schema.json` validates the real output in a
  test.
- [x] No daemon, no state write, no XDG write.
- [x] `make gates` passes; CHANGELOG/README/ticket/INDEX updated.

## Implementation map

- `src/runtime/ports/monitor_catalog.py` — sibling port; do not add an abstract
  method to `IMonitorSource`.
- `src/runtime/adapters/hyprland_monitor_source.py` — parse the full payload;
  keep `detect_monitors()` as the name projection.
- `src/runtime/application/monitors.py` (or a new pure module) — filter/order.
- `src/runtime/cli/main.py` — `monitors` Typer group + `list` using
  `_OUTPUT_FORMAT_OPTION` and `CustomView`.
- `contracts/schemas/monitors-list.schema.json` + tests.

## Evidence

Command (live shape; a fake `hyprctl` supplies two targetable outputs plus a
disabled and a mirrored output):

```sh
PATH="<fake-bin>:$PATH" dotfiles-runtime monitors list --format json
# exit 0
```

```json
{
  "schema_version": 1,
  "source": "hyprland",
  "monitors": [
    {
      "connector": "eDP-1",
      "label": "BOE 0x0BCA",
      "description": "BOE 0x0BCA",
      "make": "BOE",
      "model": "0x0BCA",
      "serial": "",
      "geometry": { "x": 0, "y": 0, "width": 1920, "height": 1080 },
      "scale": 1.0,
      "transform": 0,
      "focused": true,
      "mirror_of": null
    },
    {
      "connector": "DP-3",
      "label": "Dell U2720Q",
      "description": "Dell U2720Q",
      "make": "Dell",
      "model": "U2720Q",
      "serial": "SN3",
      "geometry": { "x": 1920, "y": 0, "width": 3840, "height": 2160 },
      "scale": 1.5,
      "transform": 0,
      "focused": false,
      "mirror_of": null
    }
  ]
}
```

Command (headless / no `hyprctl` on `PATH`):

```sh
env PATH="<empty-bin>" dotfiles-runtime monitors list --format json
# exit 0
```

```json
{
  "schema_version": 1,
  "source": "unavailable",
  "monitors": []
}
```

Tests:

```sh
uv run pytest -q tests/unit/test_hyprland_monitor_source.py \
  tests/unit/test_hyprland_monitor_catalog.py \
  tests/unit/test_cli_monitors_list.py
# 37 passed
```

`tests/unit/test_cli_monitors_list.py` validates the real command output against
`contracts/schemas/monitors-list.schema.json` with `fastjsonschema`, and
asserts no `XDG_STATE_HOME`/`XDG_CONFIG_HOME`/`XDG_DATA_HOME`/`XDG_CACHE_HOME`
path is created. `tests/unit/test_hyprland_monitor_source.py` is unchanged
(`git diff --stat` empty for that file).

Gate:

```sh
make gates
# test, lint, contracts-check, hermetic, lock-check, docs-check all pass
```

## Contract note

The capture-side consumer is `ags-capture-tool` `REQ-008` in
`tickets/EPIC-002-monitor-catalog-dropdowns/`. The runtime ticket is the
execution record for the service itself.
