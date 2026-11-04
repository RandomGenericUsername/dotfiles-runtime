---
id: REQ-008
title: "User-owned dotfiles settings with a terminal ANSI policy"
kind: requirement
status: proposed
lane: 3
links:
  openspec: none
  bmad: none
worktree: .worktrees/REQ-008/
branch: ticket/REQ-008-dotfiles-user-settings
---

# REQ-008 — User-owned dotfiles settings with a terminal ANSI policy

> **Planning only.** This ticket records a backlog requirement and an
> investigation. It does **not** implement the settings file, the ANSI policy,
> or any runtime / tpr / provisioning change. The acceptance boxes below are
> future-work criteria, intentionally unchecked.

## Why

The dotfiles desktop has no single, remembered place for user preferences.
Today a terminal's 16 ANSI colors are fully derived from the wallpaper: every
slot is a Material role selected by the theme producer (`REQ-024`, live since
tpr `v0.5.3`), with terminal chrome decoupled from slot indices by `REQ-025`
(live since `v0.5.4`). That is the only ANSI behavior available.

Before `REQ-024`, tpr shipped a different recipe (`REQ-023`, tpr `v0.5.2`):
fixed ANSI hue families tinted toward the wallpaper primary, bounded to 25° of
drift, at least 40° apart, with saturation clamped to `[0.35, 0.65]`. The owner
wants that recipe to remain available as a user choice without reverting the
current default.

The request is therefore one feature: **a user-owned dotfiles settings file**,
starting with one setting, `theme.terminal.ansi_policy`, accepting:

- `wallpaper` — current behavior; the 16 slots come directly from the selected
  mode's Material roles (`REQ-024` / `REQ-025`).
- `themed` — the earlier fixed-hue recipe tinted toward the wallpaper primary
  (`REQ-023`: 30% max tint, 25° family bound, 40° minimum separation,
  saturation `0.35–0.65`).

The default when the file or key is absent must remain `wallpaper`, so an
unmodified host behaves exactly as it does today.

A settings file is also the natural home for future non-ANSI preferences
(desktop, notifications, capture defaults). This ticket defines the ownership,
schema, precedence, validation, cache-key, and provisioning-seed rules once so
later settings do not invent competing surfaces.

## Current live behavior (2026-11-04)

These are facts about the live host, not proposals.

```text
$ dotfiles-runtime version
0.5.2

$ dotfiles-runtime inspect producer --format json
{
  "producer": "tpr",
  "source": "config",
  "engine": "native",
  "runtime": "container",
  "config_path": "/home/inumaki/.local/share/dotfiles/config/dotfiles-runtime/producer.json",
  "ambient_env": {},
  "ignored_env": {},
  "warnings": []
}

$ sed -n '1,20p' ~/.local/state/dotfiles/current/colors.sequences | cat -v
^[]4;0;#181115^[\
^[]4;1;#ffb4ab^[\
^[]4;2;#f3ba9c^[\
^[]4;3;#dfbecc^[\
^[]4;4;#fbb0d7^[\
...
^[]10;#eddfe3^[\
^[]11;#181115^[\
^[]12;#eddfe3^[\
```

Slot 1 is the theme's `error` (`#ffb4ab`), slot 4 is `primary`, and the chrome
(`10/11/12`) is `on_surface` / `surface` / `on_surface`, not slot 15. This is
the `REQ-024` role table plus the `REQ-025` chrome fix.

- tpr release: `theme-producer:pyproject.toml` version `0.5.4`; the provisioned
  spine settings pin the image to `image_tag = "v0.5.4"` (live
  `~/.local/share/dotfiles/config/theme-producer/settings.toml`).
- No general runtime settings file exists today. `$XDG_CONFIG_HOME/dotfiles/`
  is absent on this host and `desired.json` is absent too.
  Verify: `test -e ~/.config/dotfiles/settings.toml; echo $?` → `1`; same for
  `~/.config/dotfiles/desired.json`.
- `dotfiles-runtime` itself never interprets ANSI semantics: it writes the
  generated `colors.sequences` / `colors.kitty` bytes and applies/signals them
  (see §2.4).

## Investigation

### 1. Existing config surfaces and ownership

| Surface | Path | Owner | Read/write | Validation | User editable? |
|---|---|---|---|---|---|
| Palette producer | `<install>/config/dotfiles-runtime/producer.json` | provisioning (`runtime_daemon` role) | runtime reads only | strict JSON, exact keys, `schema_version` 1 | no (re-rendered) |
| Desired state | `$XDG_CONFIG_HOME/dotfiles/desired.json` | user | runtime reads only; never creates | strict JSON, `version`, unknown keys fail, symlink refused | yes |
| Clipboard retention | `$XDG_CONFIG_HOME/clipboard/config.json` | user | clipboard watcher reads | tolerant; any error falls back to defaults | yes |
| tpr tool settings | `<install>/config/theme-producer/settings.toml` | provisioning (`settings` role) | tpr reads | TOML via `config-assembler-engine`; pydantic schema | no (re-rendered `force: true`) |
| Runtime CLI override | `--producer` on palette-writing commands | caller | one-shot | enum `tpr` | yes (per invocation) |
| Runtime env | `RUNTIME__PALETTE__PRODUCER`, `RUNTIME__PALETTE__TPR_ENGINE` | host environment | runtime reports and **ignores** | reported as `ignored_env` | no authority |

Evidence:

- Producer config is versioned, strict, and runtime-owned:
  `src/runtime/application/palette_producer.py:48-57` defines
  `PRODUCER_CONFIG_SCHEMA_VERSION = 1`, the spine path
  `config/dotfiles-runtime/producer.json`, and the exact key set.
  `:118-130` resolves the path; `:146-205` parses and validates it;
  `:208-271` resolves CLI > config > default and reports ambient env as
  ignored.
- The runtime reads the installed spine only. `src/runtime/cli/main.py:137-151`
  resolves `DOTFILES_INSTALL_SPINE` or `$XDG_DATA_HOME/dotfiles`; `AGENTS.md:19`
  states the rule.
- `desired.json` is the precedent for a user-owned, XDG, read-only-to-runtime
  document: `src/runtime/adapters/desired_state_reader.py:1-21` documents the
  v1 schema and strict behavior, `:41-43` resolves
  `<config_home>/dotfiles/desired.json`, and `:51-106` validates it, refuses
  symlinks, and returns `None` when absent. `src/runtime/cli/main.py:238-247`
  wires it. It is deliberately outside `state_root` so it can be watched
  without watching runtime output.
- There is already a **subsystem** user config file: the clipboard reader uses
  `$XDG_CONFIG_HOME/clipboard/config.json` and is deliberately tolerant
  (`src/runtime/adapters/clipboard_config.py:1-12`, `:31-36`). It is
  file-only (design D5) and has no CLI/env authority.
- The runtime has no general settings file: `rg -n "settings\\.toml|settings\\.json|settings file" src/runtime`
  finds only the clipboard reader and tpr's provisioned settings path
  (`palette_producer.py:66-70`). No `settings.toml`/`settings.json` reader for
  runtime preferences exists.
- Provisioning renders tpr settings with `ansible.builtin.template` and the
  default `force: true`:
  `dotfiles-provision:ansible/roles/settings/tasks/main.yml:87-91`;
  the file list is `dotfiles-provision:ansible/roles/settings/vars/main.yml:107-110`.
  Re-provisioning therefore **overwrites** those files by design.
- The config-in-spine contract says the spine is the single home for managed
  config and that `~/.config` entries are symlinks into the spine:
  `dotfiles-v3:docs/02-config-in-spine-pattern.md:41-44`, `:64-74`, `:76-80`.
  `desired.json` is already a documented exception in code. A user settings
  file would be a second, explicit exception, not a managed config.
- tpr's own settings chain is
  `--config` path > env path > 3-level directory traversal > XDG
  (`~/.config/theme-producer/settings.toml`) > packaged default:
  `theme-producer:src/theme_producer/adapters/settings/config_resolver.py:1-5`,
  `:100-125`. The schema has `[output]`, `[generation]`, `[templates]`,
  `[runtime]`, `[container]`:
  `theme-producer:src/theme_producer/adapters/settings/schema.py:150-155`,
  domain in `theme-producer:src/theme_producer/domain/settings.py:36-113`.
  There is **no** `[ansi]` section today.
- `producer.json` is rendered by
  `dotfiles-provision:ansible/roles/runtime_daemon/templates/producer.json.j2:1`
  from `runtime_palette_producer` / `runtime_palette_tpr_engine` inputs
  (`dotfiles-provision:ansible/inventory/group_vars/all.yml:57-73`).

**Conclusion:** the runtime has a precedent for a user-owned XDG file
(`desired.json`) and a precedent for strict validation; it does not have a
general settings surface. `producer.json` and tpr's `settings.toml` are
machine-owned and overwrite-safe only because users are not expected to edit
them.

### 2. How a value would reach the palette producer

#### 2.1 The seam

The runtime does not import tpr. It invokes `tpr generate` as a subprocess with
an allowlisted environment and a fixed argv:

- `src/runtime/application/palette_producer.py:292-320` composes `TprAdapter`
  with the spine templates dir, engine/runtime, and the provisioned
  `config/theme-producer/settings.toml` path.
- `src/runtime/adapters/tpr_adapter.py:258-272` builds the child env:
  `THEME__OUTPUT__DIRECTORY`, `THEME__OUTPUT__OVERWRITE`,
  `THEME__GENERATION__ENGINE`, `THEME__RUNTIME__MODE`,
  `THEME__TEMPLATES__DIRECTORY`, and `THEME_CONFIG_FILE_PATH` when the
  provisioned tpr settings file exists.
- The argv contract is fixed in `contracts:invocation-contract.json:141-190`
  (`tpr generate <wallpaper> --format ...`; no `--config` flag). `env.py:46-52`
  already allowlists the whole `THEME__` prefix, so a new
  `THEME__ANSI__POLICY` override passes the subprocess boundary with **no**
  allowlist change and **no** argv/contract change.
- tpr's config resolver already has an env override rule for every known key
  (`theme-producer:.../config_resolver.py:125-145`) with `env_prefix="THEME"`.
  Adding `OverrideRule("ansi.policy", {CLI, ENV})` is the natural extension.
- For container runtime, tpr serializes its resolved `AppSettings` to a temp
  TOML file and mounts it as `/tpr-config/settings.toml`
  (`theme-producer:src/theme_producer/adapters/container_processor.py:35`,
  `:60-80`; serializer at
  `theme-producer:src/theme_producer/adapters/settings_serializer.py:71-133`).
  Therefore an `[ansi]` section must be added to **both** the schema/domain and
  the serializer, or the inner container invocation would silently lose it.

**Recommended flow (proposal):**

```text
$XDG_CONFIG_HOME/dotfiles/settings.toml
  theme.terminal.ansi_policy = "wallpaper" | "themed"
        │
        ▼  runtime settings resolver (new)
TprAdapter(ansi_policy=...)
        │  env override on the child process
        ▼
THEME__ANSI__POLICY="themed"
        │
        ▼  tpr config resolver rule ansi.policy
tpr AppSettings.ansi.policy
        │
        ├── local engine: domain/ansi.py chooses the recipe
        └── container engine: SettingsSerializer writes [ansi] into the temp
            settings file, then the inner tpr uses the same policy
```

Why env and not a new `--ansi-policy` argv: the argv is a pinned cross-repo
contract; env is already the documented override protocol for tpr settings and
is already allowlisted. A CLI flag on `tpr` could be added later, but it is not
needed for this feature.

#### 2.2 Cache-key seam (critical)

The palette cache key is currently a function of wallpaper + templates +
producer identity, and **does not include any setting**:

- `src/runtime/adapters/hashing.py:256-274` defines
  `palette_entry_hash(wallpaper_hash, template_set_hash, seed=None)`.
- `:277-303` defines the tpr seed:
  `sha256("tpr:" + engine + ":" + version + ":" + template_set_hash)`;
  `:306-322` wraps it as `palette_entry_hash_for_producer`.
- `src/runtime/adapters/tpr_adapter.py:203-211` computes the expected key with
  that seed; `src/runtime/application/derive.py:329-334` uses it to choose
  `cache/palettes/<ph>/` and returns a cache hit when the directory is
  complete.

If two ANSI policies produce different `colors.sequences` / `colors.kitty`
bytes but share the same `<ph>`, switching policy can hit the old entry and the
terminal never changes. The key **must** include the policy.

**Required change (proposal):** extend the tpr seed with a canonical variant
token, e.g.

```text
sha256("tpr:" + engine + ":" + version + ":variant=ansi=" + ansi_policy +
       ":" + template_set_hash)
```

Always include the token, including `ansi=wallpaper`, so the key is an explicit
function of every artifact-affecting input. This invalidates existing palette
keys once; in the same release tpr's version bump would already invalidate them
(the version is part of the seed), so the extra cost is zero. Keep the old
cache directories until normal prune; never delete them as part of a settings
change.

Evidence for the current omission:
`rg -n "ansi|policy" src/runtime/adapters/hashing.py src/runtime/adapters/tpr_adapter.py`
returns no policy input. Verify:
`uv run python -c "from runtime.adapters.hashing import palette_producer_seed; print(palette_producer_seed('tpr','a'*64,engine='native',version='v0.5.4'))"`
has no policy argument.

#### 2.3 Activation seam outside the key (critical finding)

Including the policy in the key is necessary but **not sufficient** on the
current reconcile path. `ReconcileDesktopStateUseCase._ensure_entries` takes the
persisted `state.palette.entry_hash`, checks that directory, and only calls the
derivation pipeline when that directory is incomplete or missing:

- `src/runtime/application/reconcile.py:437-449` — `palette_dir` comes from
  `palette.entry_hash`; a complete existing directory short-circuits.
- `src/runtime/adapters/invalidation.py:140-151` — the palette "input hash" is
  only `template_set_hash()`; it does not include producer version/engine or any
  policy. `src/runtime/application/check_inputs.py:64-77` compares that against
  `meta.json.input_template_hash`.

Consequences on today's code, if only §2.2 were implemented:

1. `wallpaper set` would derive the new key and write a new palette (correct).
2. `reconcile`, `reconcile --check-inputs`, and `reconcile --regenerate-stale`
   would still consider the old palette fresh, because its directory exists and
   its `input_template_hash` matches the unchanged template set.
3. A daemon converge triggered by any other input change would likewise keep
   the old palette.

**Required change (proposal):** make palette freshness compare the **full
producer-aware expected key** with `state.palette.entry_hash`, not only the
template hash. On mismatch, regenerate through the pipeline (the same path as a
cache miss) and update `current.json`; report the layer stale in
`reconcile --check-inputs` so `--regenerate-stale` and `doctor --repair` can
act. This also fixes latent drift when `producer.json` changes `tpr_engine` or
when tpr's version changes — the key already carries those values but the
freshness check does not.

The ticket does **not** require live hot-reload when the user saves the file.
Phase 1 may define activation as "the next `wallpaper set` or `reconcile`
picks it up"; see the open question. If the daemon should react without an
explicit command, the settings file must be added to the AD-39 watch roots
(`src/runtime/adapters/watch_roots.py:43-72`), which currently watch the tpr
templates dir, WEG catalog, icon templates/mappings, and optional
`desired.json` only.

#### 2.4 Terminal apply / reload path

The runtime never computes ANSI colors. It applies the producer's bytes:

- `src/runtime/adapters/terminal_color_applier.py:1-23` documents that OSC
  bytes are produced by the pinned template and the adapter is a pure reader;
  `:125-202` reads `current/colors.sequences` and writes it verbatim to
  `/dev/tty` (or the injected test tty), with the corrupt-artifact guard.
- `src/runtime/adapters/kitty_reloader.py:1-23` signals every running kitty
  with `SIGUSR1`; kitty includes the runtime `current/colors.kitty` via the
  provisioned `kitty.conf` (`dotfiles-assets:templates/kitty/kitty.conf.j2:2`).
- Fresh shells `cat` `current/colors.sequences`
  (`dotfiles-assets:templates/zsh/.zshrc.j2:33`).
- The swap sequence applies the terminal palette once from
  `current/colors.sequences` (`dotfiles-v3:_bmad-output/.../shared-data-contract.md:169`).

Therefore a policy switch needs no terminal-adapter change: once the palette
entry changes and `current/` is repointed, both reload paths carry the new
bytes. The only runtime obligations are resolving the setting, passing it to
tpr, and making the cache/freshness layers recognize the change.

### 3. AGS Preferences (comparison only)

AGS has a `Preferences` port: `get(namespace, key, fallback)` with a reactive
accessor and a file monitor (`ags-platform:ports/preferences.ts:23-38`). The
Gio adapter maps a namespace to
`$XDG_CONFIG_HOME/<namespace>/config.json`, degrades every read error to the
fallback, and watches the file (`ags-platform:adapters/preferences.ts:24-74`).
It is currently used only by the capture widget
(`ags-apps:bar/bar/widgets/recording.tsx:54-55`).

This is a good AGS-app-local preference mechanism, not a host settings owner:

- It is JSON without sections or a schema version.
- Its coercion is type-only (`coerce<T>`): a wrong value silently degrades to
  the fallback, which is wrong for a policy that changes generated artifacts.
- It has no path for the runtime or tpr to read; only AGS widgets import it.
- Adding runtime/tpr as consumers would create a TypeScript-to-Python
  configuration dependency in the wrong direction (the runtime is the host
  authority; AGS is a consumer).

**Do not use AGS Preferences as the owner.** AGS may later consume the
dotfiles setting read-only if a UI surface is wanted.

## Options

| | A. User-owned XDG TOML | B. Spine runtime settings JSON | C. Extend tpr settings.toml | D. AGS Preferences only |
|---|---|---|---|---|
| Path | `$XDG_CONFIG_HOME/dotfiles/settings.toml` | `<install>/config/dotfiles-runtime/settings.json` | `<install>/config/theme-producer/settings.toml` `[ansi]` | `$XDG_CONFIG_HOME/<namespace>/config.json` |
| Owner | user; runtime validates | provisioning renders; runtime reads | provisioning renders; tpr reads | AGS apps |
| User editability | high (real file, comments) | low (re-render overwrites) | low (`force: true` overwrites) | high for AGS only |
| Overwrite safety | provisioning seeds only if absent | re-provision overwrites edits | re-provision overwrites edits | safe for its namespace |
| Schema validation | runtime owns strict schema | runtime owns strict schema | tpr pydantic schema | none; tolerant coercion |
| Precedence fit (CLI > env > file > default) | natural | possible, but file is not user intent | possible, but file already has another purpose | no runtime/tpr precedence |
| Cache invalidation | runtime knows policy when keying | runtime knows policy when keying | tpr would need to expose it back to runtime (or runtime reads tpr file) | runtime has no access |
| Multi-repo changes | runtime + provision seed + v3 docs/pins | runtime + provision render + v3 docs/pins | tpr + runtime + provision + v3 | ags-platform (+ runtime bridge, if any) |
| "One dotfiles settings file" goal | directly satisfied | only for machine config | no; splits user preference across tools | no |
| Verdict | **recommended** | viable but wrong ownership | stepping stone only | reject |

### A — User-owned typed file at `$XDG_CONFIG_HOME/dotfiles/settings.toml` (recommended)

The runtime reads and validates a user-owned TOML file; provisioning seeds a
commented default **only when the file does not exist** and never overwrites
it. Runtime maps `theme.terminal.ansi_policy` to tpr's env override.

Pros: the file is genuinely user-editable; it sits next to `desired.json`, the
existing user-intent document; provisioning cannot clobber user edits; the
runtime owns one schema and one precedence rule; future sections stay in one
place. Cons: outside the spine containment invariant, so the docs must record
the exception (as they already do in code for `desired.json`); provisioning
needs one new seed task; runtime needs a TOML reader, validator, inspect
surface, and key/freshness changes.

### B — Spine-owned `config/dotfiles-runtime/settings.json`

This mirrors `producer.json`: provisioning renders it, every runtime root reads
it, and the spine stays the single machine config tree. It is a good pattern
for **machine-owned** values. It fails the user-owned requirement: the settings
role renders with `force: true`, so a re-provision reverts edits, and changing a
preference means editing deployment inputs rather than a dotfile. JSON also
loses comments and section readability. It would still need almost all of the
runtime work in A.

### C — Extend tpr's `settings.toml` with `[ansi] policy`

Simplest for ANSI alone: tpr already has a settings chain, a schema, and
container serialization. But the provisioned spine file is rendered with
`force: true` (`dotfiles-provision:.../settings/tasks/main.yml:87-91`), so the
user cannot durably edit it. It also puts a user policy in a tool-owned file and
does not produce "one dotfiles settings file"; the runtime would either read
tpr's machine file (coupling, wrong ownership) or duplicate the value through
env. It does not model future runtime/desktop settings at all.

### D — AGS Preferences only

Cannot configure runtime or tpr host behavior; see §3. Rejected.

## Recommendation

**Adopt option A.** Add a user-owned, versioned TOML file at
`$XDG_CONFIG_HOME/dotfiles/settings.toml` (default
`~/.config/dotfiles/settings.toml`). The runtime reads it read-only with
`tomllib` (stdlib on Python ≥ 3.14; `pyproject.toml:8`), validates it strictly,
resolves the ANSI policy with CLI > env > file > default, and passes the policy
to tpr through the existing `THEME__` env seam. Provisioning seeds the file only
when absent. `producer.json` remains the runtime-owned machine config; the new
file is user intent, exactly like `desired.json`.

Reasons, tied to evidence:

1. It is the only option that is both user-editable and durable across
   re-provisioning; the current `force: true` tpr render and the
   `producer.json` render both overwrite by design.
2. It reuses an existing, proven runtime pattern: `desired.json` is already a
   strict, optional, symlink-refusing, read-only XDG document
   (`desired_state_reader.py:51-106`), and the clipboard proves the runtime can
   own a subsystem config (though that one is tolerant by design).
3. It keeps the "one settings file" goal in the place users actually edit,
   while tpr's provisioned settings stays a machine/tool artifact.
4. It maps to tpr with no argv-contract change: `THEME__ANSI__POLICY` is
   already allowed by `env.py:46-52`, and tpr's resolver already supports
   `THEME__SECTION__KEY` overrides.
5. It keeps cache-key ownership in the runtime, where the palette key is
   computed (`tpr_adapter.py:203-211`, `hashing.py:277-322`).

## Proposed shape (design only — not implemented)

### Path and format

```text
$XDG_CONFIG_HOME/dotfiles/settings.toml
# default: ~/.config/dotfiles/settings.toml
```

- **TOML**, not JSON: comments, sections, and stdlib `tomllib`; users already
  edit tpr/weg/itr `settings.toml` files, so the format is familiar. The
  existing `desired.json` stays JSON; it is a declarative intent document, not
  a preference file.
- The file is a **real user file**, not a symlink into the spine. Refuse
  symlinks (mirror `desired_state_reader.py:57-69`) so a redirectable settings
  file cannot spoof policy.
- The runtime never creates or writes it; only provisioning seeds it and the
  user edits it. `inspect` and `doctor` are read-only.

### Draft schema

```toml
# $XDG_CONFIG_HOME/dotfiles/settings.toml
schema_version = 1

[theme.terminal]
# "wallpaper" = 16 slots from Material roles (REQ-024/025; current behavior)
# "themed"    = fixed ANSI hue anchors tinted toward the wallpaper primary
#               (REQ-023 recipe)
ansi_policy = "wallpaper"
```

Rules:

- `schema_version` is required when the file exists and must be the integer
  `1` (booleans are not integers).
- `[theme.terminal]` is the only section defined in schema version 1.
- `ansi_policy` is optional; absent → `wallpaper`.
- Unknown top-level keys, unknown sections, and unknown keys inside
  `[theme.terminal]` fail loudly with the file path and offending key. This is
  the `desired.json` philosophy (`desired_state_reader.py:9-21`), not the
  clipboard's tolerant fallback: a wrong policy changes generated artifacts, so
  silently degrading is worse than stopping.
- Values are trimmed and lowercased before the enum check; anything outside
  `{"wallpaper", "themed"}` fails with the accepted set.

### Precedence

| Rank | Source | Exact form | Notes |
|---|---|---|---|
| 1 | CLI | `--ansi-policy wallpaper\|themed` | One-shot; add to `wallpaper set`, `icons regenerate`, `reconcile`, `doctor` (the commands that already accept `--producer`; `rg -n "producer: str \\| None = typer.Option" src/runtime/cli/main.py` → lines 849, 1060, 1606, 2957). |
| 2 | Env | `RUNTIME__THEME__TERMINAL__ANSI_POLICY` | One-shot/testing override. Never set by provisioning or systemd. Unlike `RUNTIME__PALETTE__*`, this is a deliberate user override, not a stale authority; it is reported by `inspect settings` as source `env`. |
| 3 | User file | `$XDG_CONFIG_HOME/dotfiles/settings.toml` → `theme.terminal.ansi_policy` | The durable setting. |
| 4 | Composition default | `wallpaper` | Current behavior; no file/key needed. |

The daemon has no CLI, so it resolves env > file > default. If a stale
`RUNTIME__THEME__TERMINAL__ANSI_POLICY` in a long-lived environment is a concern,
the daemon may ignore source rank 2 (file only) — record that in the
implementation design. Provisioning must not write this env var anywhere.

### Default and absent behavior

- File absent: `wallpaper`, source `default`, no warning (this is the normal
  state on an unmodified host). `inspect settings` exits 0 and reports
  `exists: false`.
- File present but `[theme.terminal]` absent: `wallpaper`, source `default`.
- Key absent: `wallpaper`, source `default` (or `file` with the default value?
  Report `default` so the provenance is honest).
- `themed`: only after tpr ships the recipe; until then the runtime pin must be
  new enough that `themed` is accepted. Version-skew handling: if the runtime
  can pass `themed` but tpr rejects it, the tpr failure must surface as a
  palette failure naming the policy, never a silent fallback. Pin updates are
  part of the implementation ticket (§ Implementation breakdown).

### Validation / error behavior

- Malformed TOML: `ValueError` naming path and parse error; exit non-zero.
- Wrong `schema_version`: `ValueError` naming expected value and got value.
- Wrong type for `ansi_policy` (integer, array, etc.): `ValueError` naming key,
  path, and expected string enum.
- Unknown key/section: `ValueError` naming path and key (fail closed on typos).
- Symlink: refused; `ValueError` naming path.
- Unreadable file: `ValueError` naming path and OS error.
- A malformed settings file must fail **before** any palette cache read or tpr
  invocation, matching the producer config's fail-loud contract
  (`palette_producer.py:20-21`).

### Inspection and doctor

Proposed read-only command:

```text
dotfiles-runtime inspect settings --format json
```

```json
{
  "path": "/home/user/.config/dotfiles/settings.toml",
  "exists": true,
  "schema_version": 1,
  "ansi_policy": "wallpaper",
  "ansi_policy_source": "file",
  "warnings": []
}
```

- Absent file → exit 0, `exists: false`, `ansi_policy: "wallpaper"`,
  `source: "default"`.
- Invalid file → exit 1, structured error naming path/key (same shape as
  `inspect producer`; see `src/runtime/cli/main.py:3782-3850`).
- This is the primary provenance surface; it does not mutate or create files.

`doctor` should gain a settings check:

- Valid or absent file → `ok` (absent is not drift).
- Malformed file → `diverged`, naming the key/path.
- Policy vs live palette mismatch → `diverged`, with detail "current palette
  was generated for policy X; resolved policy is Y; run wallpaper set or
  doctor --repair". `doctor --repair` may regenerate through the same path as
  `reconcile`; it must not silently rewrite the user file.
- Do not add `ansi_policy` to the palette `meta.json` schema in phase 1. The
  key already encodes it. Adding a field would require the `contracts`
  meta-schema (`contracts:schemas/meta.schema.json`, `additionalProperties:
  false`) and a cross-repo contract release; that is avoidable scope.

### Provisioning seed semantics

- Add a task in the external `dotfiles-provision` `settings` role (or a
  dedicated `runtime_settings_seed` role) that writes a commented default to
  `$XDG_CONFIG_HOME/dotfiles/settings.toml` **only if absent**.
- Use `ansible.builtin.template` (or `copy`) with `force: false`, or an explicit
  `stat` guard; do not add it to `settings_files` because that loop renders
  with `force: true` (`dotfiles-provision:.../settings/tasks/main.yml:87-91`)
  and would overwrite user edits.
- Ensure the parent `$XDG_CONFIG_HOME/dotfiles/` exists with `file state:
  directory`; the runtime does not create it.
- Never write the file via the `config_links` symlink pattern. The existing
  contract says `~/.config` entries are symlinks into the spine
  (`dotfiles-v3:docs/02-config-in-spine-pattern.md:64-74`); this file is a
  documented user-owned exception, alongside `desired.json`.
- Verification: if the file exists after provisioning, parse it with the
  runtime schema or `tomllib`; do not require the file to exist (deleting it is
  a valid "use defaults" action and runtime handles absence).
- Idempotence: re-running provisioning must not change the file's mtime or
  content. A test should pre-create a file with `ansi_policy = "themed"`,
  run the settings role twice, and assert it still says `themed`.

### Generalization and non-goals

Schema version 1 is deliberately one namespaced section:

```toml
schema_version = 1

[theme.terminal]
ansi_policy = "wallpaper"
```

Future settings should add namespaced sections (`[desktop]`,
`[notifications]`, `[capture]`, ...) under the same schema and resolver.
Explicit non-goals for this ticket:

- No free-form plugin/extension settings and no arbitrary key/value bag.
- No per-app or per-monitor overrides (an ANSI policy is global to the
  session; per-monitor ANSI has no consumer today).
- No writing user settings from the runtime, CLI, or daemon.
- No live file watcher/hot reload in phase 1 unless separately accepted.
- No changes to `producer.json`'s ownership or contents.
- No re-opening `REQ-024` as the default; `wallpaper` stays the default.
- No AGS preference UI in phase 1.

## Acceptance criteria (future work — unchecked)

- [ ] `$XDG_CONFIG_HOME/dotfiles/settings.toml` is read as TOML with
      `schema_version = 1` and `[theme.terminal] ansi_policy`.
- [ ] Absent file, absent section, and absent key all resolve to `wallpaper`
      with source `default`; unmodified hosts are byte-identical in behavior.
- [ ] Invalid TOML, wrong version, wrong type, unknown key/section, symlink, or
      unreadable file fails loudly before any cache read/tool invocation,
      naming the path and key.
- [ ] Precedence is exactly CLI > `RUNTIME__THEME__TERMINAL__ANSI_POLICY` >
      user file > `wallpaper`; `inspect settings --format json` reports the
      resolved value and source.
- [ ] `wallpaper set`, `icons regenerate`, `reconcile`, and `doctor` accept
      `--ansi-policy`; the daemon resolves env > file > default.
- [ ] The palette cache key includes the ANSI policy (via a producer variant
      token); a test proves `wallpaper` and `themed` for the same wallpaper +
      templates produce different `<ph>` values, and an A→B→A switch returns to
      the original key.
- [ ] `reconcile` and `reconcile --check-inputs` compare the full
      producer-aware expected palette key with `current.json`, not only
      `input_template_hash`; a policy switch is reported stale and
      `--regenerate-stale` / `doctor --repair` rebuilds the palette.
- [ ] `themed` reproduces `REQ-023`'s recipe exactly: six anchors
      (red 25°, green 145°, yellow 90°, blue 260°, magenta 330°, cyan 200°),
      30% max tint, 25° family bound, 40° minimum circular separation,
      saturation `[0.35, 0.65]`, achromatic fallback `0.50`, dark/light
      lightness `0.55/0.72` and `0.45/0.60`; slots 0/7/8/15 keep the
      `REQ-023` role mapping (surface, on_surface_variant, outline_variant,
      on_surface).
- [ ] `themed` keeps `REQ-025` terminal chrome: background/selection from
      `surface`, foreground/cursor/selection_foreground from `on_surface`, for
      both policies. Chrome is never derived from a slot index.
- [ ] `wallpaper` reproduces `REQ-024`'s slot→role table exactly and existing
      goldens remain valid for the default policy.
- [ ] tpr's `SettingsSerializer` writes `[ansi]` into the container temp
      settings so `themed` also works in container runtime mode.
- [ ] Provisioning seeds `$XDG_CONFIG_HOME/dotfiles/settings.toml` only when
      absent, never overwrites, and a pre-existing `themed` file survives two
      provisioning runs unchanged.
- [ ] `doctor` reports malformed settings and policy/palette mismatch without
      mutating the user file; absent settings is `ok`.
- [ ] Runtime and tpr README/CHANGELOG/spec/architecture docs describe the
      policy and the user-file exception; `dotfiles-v3` docs and pin checks are
      updated.
- [ ] All affected repos' gates are green (runtime: `make gates`; tpr:
      `make gates`; provision: `make gates`; v3: `make gates-ci &&
      make deploy-coverage`), with release tags and pin bumps.

## Implementation breakdown (for later — not executed here)

All items are unchecked and belong to follow-up tickets.

### theme-producer

- [ ] Add `AnsiSettings(policy: str = "wallpaper")` to `AppSettings`
      (`domain/settings.py:107-113`), a `policy` field with enum validation in
      the boundary schema (`adapters/settings/schema.py:150-155`), and an
      `OverrideRule("ansi.policy", {CLI, ENV})` in
      `adapters/settings/config_resolver.py:126-145`.
- [ ] Restore the `REQ-023` recipe from commit `e0d9adf`
      (`src/theme_producer/domain/ansi.py`) as the `themed` branch, keyed by
      the resolved policy; keep the `REQ-024` role table as the `wallpaper`
      branch.
- [ ] Sort slots 0/7/8/15 and the bright block exactly as `REQ-023` did for
      `themed`; keep `REQ-025` semantic chrome for both branches.
- [ ] Add `[ansi]` to `SettingsSerializer` so container mode carries the
      policy (`adapters/settings_serializer.py:71-133`).
- [ ] Add unit tests for both recipes, policy precedence, container
      serialization, and goldens for a representative wallpaper per policy.
- [ ] Version bump (new feature → minor, e.g. `0.6.0`), CHANGELOG, and docs;
      `make gates` green.

### dotfiles-runtime

- [ ] Add `adapters/user_settings.py` (or equivalent) that resolves, parses
      (`tomllib`), and strictly validates the file; keep I/O in `adapters/`
      and the resolved value pure.
- [ ] Add a settings domain model (`schema_version`, `ansi_policy`, source) and
      composition-root resolver; no new third-party dependency.
- [ ] Add `--ansi-policy` to `wallpaper set`, `icons regenerate`, `reconcile`,
      and `doctor`; wire env `RUNTIME__THEME__TERMINAL__ANSI_POLICY`.
- [ ] Thread the policy into `TprAdapter` (constructor + `generate` env
      override `THEME__ANSI__POLICY`).
- [ ] Extend `palette_producer_seed`/`palette_entry_hash_for_producer` with the
      canonical `variant=ansi=<policy>` token; add hash tests.
- [ ] Fix the freshness path: `ReconcileDesktopStateUseCase._ensure_entries`
      and the stale/check-inputs path compare the expected full key; add tests
      for policy change detection and A→B→A.
- [ ] Add `inspect settings` (plain/json) and a `doctor` settings check;
      include source/provenance in output.
- [ ] Add tests for absent file, absent key, malformed TOML/type/version,
      unknown key, symlink refusal, CLI/env/file precedence, and no-write
      behavior.
- [ ] Version bump, CHANGELOG, README/settings documentation; `make gates`
      green.

### dotfiles-provision

- [ ] Add the seed-only task/template for
      `$XDG_CONFIG_HOME/dotfiles/settings.toml` with `force: false` (or an
      explicit stat guard); do **not** add it to `settings_files`.
- [ ] Ensure the parent directory exists; never symlink it from the spine.
- [ ] Add idempotence tests: absent → seeded; existing `themed` → unchanged
      across two runs; malformed existing file → provisioning does not rewrite
      it (runtime will report the error).
- [ ] Verify only "if present, parses"; absence is valid.
- [ ] Pin the tpr and runtime releases that implement the policy; `make gates`
      green.

### dotfiles-v3

- [ ] Bump `dotfiles/provisioning/cli-tools.yaml` pins for tpr and
      dotfiles-runtime; update `installer.pin` if the engine changes.
- [ ] Update `docs/02-config-in-spine-pattern.md` to name
      `$XDG_CONFIG_HOME/dotfiles/settings.toml` as a user-owned exception
      (alongside `desired.json`), and `ARCHITECTURE.md` §7.3 to describe the
      runtime's user-settings resolution.
- [ ] Update `shared-data-contract.md`'s palette key row if it is still treated
      as live (currently line 137 still names the retired CSG templates dir —
      stale; correct it to the tpr adapter's seed and add the policy variant).
- [ ] Update the spec/architecture wording for CAP-008 / AD-6 if they still
      describe a single ANSI recipe.
- [ ] Optional later: an AGS consumer or preferences UI that reads the same
      user file read-only; do not create a second settings store.
- [ ] `make gates-ci && make deploy-coverage` green.

## Risks and open questions

### Risks

1. **Cache-key/freshness split.** The key change alone does not rebuild on
   `reconcile`: the current path trusts `state.palette.entry_hash` and
   `input_template_hash` (`reconcile.py:437-449`,
   `invalidation.py:140-151`). Both key and freshness must land together, or a
   policy change will appear to do nothing until the next `wallpaper set`.
2. **Provisioning overwrite.** Reusing the existing `settings` role's
   `force: true` loop would silently revert user edits. The seed task must be
   separate and absent-only.
3. **Container serialization.** Adding `[ansi]` only to the resolver is not
   enough for container mode; `SettingsSerializer` must emit it
   (`container_processor.py:60-80`). A local-only test would miss this.
4. **`themed` is a superseded decision.** `REQ-023` shipped in `v0.5.2` and was
   replaced in `v0.5.3`; restoring it is a new product decision, not a revert.
   It must be an explicit alternative, with the `REQ-024` default preserved.
5. **Chrome drift.** `REQ-023` predates `REQ-025`. Restoring the old recipe
   must not restore slot-index chrome; foreground/cursor must stay
   `on_surface` (`REQ-025`).
6. **One-time cache invalidation.** Including `ansi=wallpaper` in the seed
   changes every palette key. This is acceptable because the tpr version bump
   changes the seed anyway; old entries remain until prune and are never part of
   the live pointer.
7. **Contract drift.** `dotfiles-v3`'s shared-data-contract still describes the
   CSG templates dir and the old two-input palette key. If it is authoritative,
   update it; if it is historical, mark it so. Do not let a stale doc set the
   implementation.

### Open questions

1. **Activation timing:** must saving the file take effect live (daemon watches
   it and re-derives), or is "next `wallpaper set` / `reconcile`" acceptable?
   Phase 1 recommendation is explicit activation only; the watch-root change is
   a separate decision.
2. **`themed` semantics:** restore `REQ-023` exactly, including its slots
   0/7/8/15 role mapping and bright variants, or define a new "fixed
   ANSI-semantic" recipe informed by it? The acceptance above assumes exact
   restoration plus `REQ-025` chrome.
3. **File ownership/path:** is the user-owned
   `$XDG_CONFIG_HOME/dotfiles/settings.toml` (outside the spine) acceptable as a
   documented exception, or must all settings remain in the spine? If the spine
   is mandatory, option B wins, but user edits need a different write path.
4. **Seed policy:** should provisioning create the file on every fresh machine,
   or only when the user opts in? The request says "seed a default only when
   absent"; confirm that re-provisioning should recreate it after a user deletes
   it.
5. **Freshness scope:** should the same fix cover the existing producer
   engine/version drift (key changes but reconcile does not rebuild), or be a
   separate bug ticket? Combining is cleaner but widens the change.
6. **Per-monitor/app overrides:** explicitly deferred. If ANSI ever needs
   per-terminal-emulator behavior, that is a different setting and a different
   consumer, not a free-form extension of this file.

## Cross-references

Theme producer (current live behavior and prior recipe):

- `theme-producer:tickets/req-023-themed-ansi-anchors/ticket.md` — option B
  recipe; shipped in `v0.5.2`, `superseded_by: REQ-024`.
- `theme-producer:tickets/req-024-role-derived-ansi/ticket.md` — option C;
  every slot maps to a Material role; shipped in `v0.5.3`.
- `theme-producer:tickets/req-025-terminal-chrome-semantic-roles/ticket.md` —
  fixes the slot-15 chrome regression; shipped in `v0.5.4`.
- `theme-producer:src/theme_producer/domain/ansi.py:1-90` — current live
  `ANSI_SLOT_ROLES` table.
- `theme-producer:src/theme_producer/domain/ansi.py@e0d9adf:60-175` — the
  `REQ-023` recipe to restore behind `themed` (constants and helpers).
- `theme-producer:CHANGELOG.md:3-46` — `v0.5.4`/`v0.5.3`/`v0.5.2` records.

Runtime:

- `src/runtime/application/palette_producer.py` — producer resolution and
  composition.
- `src/runtime/adapters/tpr_adapter.py` — env seam and expected key.
- `src/runtime/adapters/hashing.py` — palette key and producer seed.
- `src/runtime/application/derive.py` — palette cache population.
- `src/runtime/application/reconcile.py` — freshness short-circuit.
- `src/runtime/adapters/invalidation.py` — stale check.
- `src/runtime/adapters/desired_state_reader.py` — user-owned XDG file
  precedent.
- `src/runtime/adapters/clipboard_config.py` — subsystem user config
  precedent.
- `src/runtime/adapters/terminal_color_applier.py` — OSC apply.
- `src/runtime/adapters/kitty_reloader.py` — kitty reload.
- `src/runtime/cli/main.py:3782-3850` — `inspect producer` pattern for the
  proposed `inspect settings`.

Provisioning / composition:

- `dotfiles-provision:ansible/roles/settings/tasks/main.yml:87-91` — current
  `force: true` settings render.
- `dotfiles-provision:ansible/roles/settings/vars/main.yml:107-110` — rendered
  settings list.
- `dotfiles-provision:ansible/roles/runtime_daemon/templates/producer.json.j2` —
  spine config precedent.
- `dotfiles-provision:ansible/inventory/group_vars/all.yml:57-73` — producer
  provisioning inputs.
- `dotfiles-v3:docs/02-config-in-spine-pattern.md:41-80` — spine containment
  and symlink contract.
- `dotfiles-v3:ARCHITECTURE.md:355-358`, `:476-494` — settings ownership and
  config resolution chain.
- `dotfiles-v3:_bmad-output/planning-artifacts/architecture/architecture-dotfiles-repo-v3-2026-08-18/shared-data-contract.md:137`
  — stale CSG palette-key row; update when the policy lands.
- `dotfiles-assets:templates/kitty/kitty.conf.j2:2`,
  `dotfiles-assets:templates/zsh/.zshrc.j2:33` — terminal artifact consumers.

AGS comparison:

- `ags-platform:ports/preferences.ts:23-38`.
- `ags-platform:adapters/preferences.ts:24-74`.
- `ags-apps:bar/bar/widgets/recording.tsx:54-55` — sole live consumer.

## Planning evidence / verify

- No production file, settings file, or code was created or modified by this
  ticket. Only `tickets/REQ-008-dotfiles-user-settings/ticket.md` and the
  `tickets/INDEX.md` rollup entry are added.
- Evidence: every path/line above was read in the working tree or the sibling
  pinned repos on 2026-11-04.
- Verify the live facts:

  ```sh
  dotfiles-runtime version                    # → 0.5.2
  dotfiles-runtime inspect producer --format json
  sed -n '1,20p' ~/.local/state/dotfiles/current/colors.sequences | cat -v
  test -e ~/.config/dotfiles/settings.toml; echo $?   # → 1 (absent)
  cat ~/.local/share/dotfiles/config/theme-producer/settings.toml  # image_tag v0.5.4
  cd theme-producer && git show e0d9adf:src/theme_producer/domain/ansi.py | sed -n '60,175p'
  ```

- Verify the code/vs-doc discrepancy:

  ```sh
  cd dotfiles-runtime
  rg -n "ansi|policy" src/runtime/adapters/hashing.py src/runtime/adapters/tpr_adapter.py
  # → no ANSI policy input today
  rg -n "input_template_hash|template_set_hash" src/runtime/adapters/invalidation.py
  # → palette stale-check compares templates only
  ```

- Suggested gate for this planning change: `make docs-check` (ticket files do
  not affect package code). Full `make gates` is run before merging any future
  implementation, not for this planning-only ticket.
