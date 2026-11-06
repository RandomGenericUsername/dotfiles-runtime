# skin-derivation Specification

## Purpose

Define the palette-independent skin derivation layer: selection from desired state, deterministic
template expansion, content-addressed cache identity, atomic pointers, appearance-hash
participation and fail-closed behavior.

Source: FROZEN SKIN SPEC v1 (owner, GTK 4.24 probes); existing palette/effects/icons layer
contracts in `src/runtime/application/derive.py`, `src/runtime/adapters/cache.py`,
`src/runtime/adapters/consumer_path_spec.py`.

## ADDED Requirements

### Requirement: skin selection

The runtime SHALL read an optional `skin` string from `desired.json`, default to `glass`, and
reject a non-string or empty/invalid name loudly with the document path and field.

#### Scenario: no skin selected

- **WHEN** `desired.json` has no `skin` key
- **THEN** the resolved skin name is `glass`

#### Scenario: malformed skin

- **WHEN** `desired.json` contains `"skin": 3`
- **THEN** reading fails with `ValueError` naming the path and `skin`

### Requirement: deterministic expansion

The skin adapter SHALL accept JSON or TOML manifests with `name`, `version: 1`, `tokens` and a
non-empty `apps` map, substitute every `{{SKIN_<TOKEN>}}` occurrence deterministically, reject
unresolved placeholders, and emit one `<app>.css` per app plus a flat `skin.json` token map.

#### Scenario: unresolved token

- **WHEN** a template contains `{{SKIN_MISSING}}` and the manifest has no `missing` token
- **THEN** expansion fails with an error naming the template and token; nothing reaches the cache

### Requirement: palette-independent cache identity

The skin entry hash SHALL be `sha256(templates_hash ‖ manifest_hash)`. Wallpaper and palette
changes SHALL NOT change it; template or manifest changes SHALL.

#### Scenario: wallpaper changes

- **WHEN** a skin was expanded and only the wallpaper/palette changes
- **THEN** the next derivation is a cache hit and no expansion runs

#### Scenario: template changes

- **WHEN** a template file changes
- **THEN** the next derivation is a cache miss at a new hash

### Requirement: atomic pointers

A successful derivation SHALL publish `current/skins/<app>.css` and `current/skin.json` as
atomic symlinks into `cache/skins/<hash>/`, and the AGS consumer pointers
`config/ags/skin.css` / `config/ags/skin.json` SHALL resolve to those `current/` artifacts.

#### Scenario: repoint

- **WHEN** a new skin entry is published
- **THEN** both current links resolve to the new entry and no temporary link remains

### Requirement: fail-closed per skin

A missing/invalid manifest or template SHALL keep the previous skin, log a structured error, and
SHALL NOT touch app CSS or consumer pointers.

#### Scenario: invalid manifest after a good skin

- **WHEN** reconcile runs with a corrupt manifest and the previous skin is cached
- **THEN** `current.json` and the AGS skin pointers still name the previous skin

### Requirement: appearance participation

The appearance hash SHALL include the skin entry hash so a skin change refreshes live apps
through the existing `palette-refresh` lane; the restart-only bar lane SHALL be unchanged.

#### Scenario: skin-only change

- **WHEN** only the skin entry hash changes
- **THEN** `appearance_hash_for_state` returns a different hash
