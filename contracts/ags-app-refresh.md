# AGS app refresh protocol — `palette-refresh`

The fixed request/response protocol between `dotfiles-runtime` and a running
**live** standalone AGS app after an appearance change. The runtime is the only
palette-refresh trigger (AD-22/AD-27); apps do not invent a palette event and do
not subscribe to `wallpaper.state` for palette application.

This document is the language-neutral protocol source. The runtime-owned result
record is defined by
[`schemas/ags-app-refresh-result.schema.json`](./schemas/ags-app-refresh-result.schema.json);
the app identity is defined by
[`schemas/ags-app-manifest.schema.json`](./schemas/ags-app-manifest.schema.json).

## Invocation

The runtime invokes each running live app after the palette repoint and the
appearance-ledger gate:

```sh
ags request palette-refresh <appearance-hash> -i <instance>
```

- `palette-refresh` is the fixed command name (`argv[0]` in the handler).
- `<appearance-hash>` is the only argument (`argv[1]`): an opaque, non-empty
  string identifying the applied appearance (palette + icons + effects +
  icon-manifest inputs; AD-29). It is not a path.
- `-i <instance>` selects the AGS instance; it is caller syntax, not a handler
  argument.
- The handler receives exactly `["palette-refresh", "<appearance-hash>"]`. Any
  other command or argument count is malformed and must not be treated as a
  successful refresh.

Restart apps do **not** implement `palette-refresh`; they are handled by the
serialized restart lane (AD-28/AD-30). A failed live refresh never becomes a
restart.

## Transport — AGS 3.1.0 string response

AGS 3.1.0's request transport is a **string**, not a structured object. The app
registers a `requestHandler(argv: string[], res: (response: string) => void)`
and answers by calling:

```ts
res(JSON.stringify({ ok: true }))
```

The handler must call `res` **exactly once**, after the refresh has fully
completed, with a string produced by `JSON.stringify`. A JSON object literal
(`res({ ok: true })`) is forbidden: AGS prints the response with
`cmd.print_literal(str + "\n")`, so a non-string becomes the string
`[object Object]` on stdout.

The caller reads **one JSON-encoded string on stdout, exactly once** — one
line, with no other stdout output (AGS adds the trailing newline). Progress
logs, debug output and app-internal logging go to stderr or the app's own log
file, never to the request response.

A handler that is absent produces the non-JSON string
`instance "<name>" has no request handler implemented`; the runtime treats that
as a malformed response (failure), not a silent skip.

## Permitted responses

| Meaning | Response string | Required fields |
| --- | --- | --- |
| Applied | `{"ok":true}` | `ok` is `true`; no `deferred` key. The refresh has fully completed. |
| Deferred | `{"ok":true,"deferred":"reason"}` | `ok` is `true`; `deferred` is a non-empty reason. Accepted but not applied yet. |
| Failed, retryable | `{"ok":false,"error":"reason","retryable":true}` | `ok` is `false`; `error` is non-empty; `retryable` is `true`. |
| Failed, terminal | `{"ok":false,"error":"reason","retryable":false}` | `ok` is `false`; `error` is non-empty; `retryable` is `false`. |

`ok: true` without `deferred` means **applied**, not scheduled: no asynchronous
work may remain after the response. If the app cannot finish within the caller
timeout, it must return `deferred` (or `busy`) instead of `ok: true`.

No other shape is valid. In particular, extra keys, a non-boolean `ok`, a
`deferred` key with `ok: false`, a failed response without `retryable`, or an
object literal are malformed. A concurrency conflict is the retryable failure
`{"ok":false,"error":"busy","retryable":true}`.

## Timeout and failures

- The runtime enforces a **5 s wall-clock timeout per live app** (AD-27/AD-28).
  The timeout covers the transport and the handler's completion; a handler that
  cannot finish returns `deferred`/`busy` before the deadline.
- Malformed, empty or extra output, a JSON object literal, non-zero exit, or the
  5 s timeout is a **failure** for that app.
- One app's failure does not stop the other apps in the parallel live lane.
- There is no immediate retry within the operation and no restart fallback.
  The applied-appearance ledger records `failed`/`deferred`; the next trigger
  retries those apps (AD-29). A terminal (`retryable:false`) failure is recorded
  as such and is not treated as applied.
- The runtime, not the app, emits the per-app result/log record. App-internal
  logs are separate.

## Idempotency

- Refreshing with the same appearance hash is safe and repeatable. If the app is
  already at that appearance, return `{"ok":true}` without destructive side
  effects.
- The handler is re-entrant. While another refresh is in progress, return
  `{"ok":false,"error":"busy","retryable":true}`. Do not queue unbounded work or
  block the AGS event loop.
- An interrupted or deferred refresh may be retried with the same hash; the
  handler must converge to the requested appearance, not toggle relative state.
- The runtime calls the app only while it is running. Startup application of the
  current palette remains the app's own responsibility; the protocol only covers
  a palette change after startup.

## State preservation

`palette-refresh` is non-destructive:

- Apps with `holdsUnsavedState: true` must preserve pending edits. If applying
  the new palette would discard or invalidate them, return `deferred` with the
  reason (or `busy` while a save/preview is in flight); never quit, restart or
  reset the app.
- Apps with an active transient surface (open dialog, draft preview, in-flight
  operation) preserve it, or return `deferred`/`busy`.
- Writable state lives under `$XDG_CONFIG_HOME/` or `$XDG_STATE_HOME/`, never
  inside an installed tag (AD-37). A refresh updates the stable palette pointer
  or in-memory state; it does not write into `<install>/apps/<installDir>/<tag>/`.
- `wallpaper.state` is the progress/UI event only. It is not the palette-refresh
  trigger; consumers use it for progress and status, not for palette
  application (AD-27).

## Runtime result record

The runtime writes one JSON Lines record per app per operation (AD-28), defined
by `schemas/ags-app-refresh-result.schema.json`. The record carries:

- identity/timing: `runId`, `app`, `instance`, `configDir`, `mode`,
  `appearanceHash`, `startedAt`, `endedAt`, `durationMs`, `discoveryIndex`;
- outcome: `status` (`applied|deferred|failed|skipped`), `reason`, `retryable`;
- invocation evidence: `argv`, `exitCode`, parsed `response`, `error`,
  `timeout`.

`skipped` is runtime-side only (the ledger says the app is current); it has no
invocation and therefore carries a `reason` but no successful response. The
summary is ordered by ascending `discoveryIndex`, not completion order.

## Evidence

- AGS 3.1.0 CLI/JS package: `ags version 3.1.0`; `/usr/share/ags/js/package.json`
  declares `3.1.2`.
- String transport and no-handler response:
  `/usr/share/ags/js/lib/gtk4/app.ts:272-296` —
  `request(args: string[], response: (response: string) => void)`,
  `cmd.print_literal(str + "\n")`, and
  `instance "<name>" has no request handler implemented`.
- Fixed command, argument and response shapes: architecture spine AD-27
  (`ags request palette-refresh <appearance-hash> -i <instance>`; one
  JSON-encoded string; applied/deferred/failed; 5 s timeout).
- Result/log fields: AD-28 (runtime-owned JSON Lines, identity, timing, argv,
  exit, response, error, timeout, discovery index).
- Ledger statuses/retry: AD-29 (`applied|deferred|failed|skipped`; retry
  failed/deferred on the next trigger; one operation lock).

Verify:

```sh
ags --version
grep '"version"' /usr/share/ags/js/package.json
sed -n '272,296p' /usr/share/ags/js/lib/gtk4/app.ts
sed -n '113,135p' ../ags-apps/_bmad-output/initiative-ags-standalone-apps/architecture-standalone-app-contract/architecture-standalone-app-contract.md
sed -n '136,156p' ../ags-apps/_bmad-output/initiative-ags-standalone-apps/architecture-standalone-app-contract/architecture-standalone-app-contract.md
sed -n '157,176p' ../ags-apps/_bmad-output/initiative-ags-standalone-apps/architecture-standalone-app-contract/architecture-standalone-app-contract.md
```
