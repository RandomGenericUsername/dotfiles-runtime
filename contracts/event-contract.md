# Event Contract — Runtime ↔ Shell

Companion to `ARCHITECTURE-SPINE.md` (Phase 5). This is the **only** thing the
Python runtime world and the GJS/AGS shell world share — neither imports the
other's code; both obey this document. A rename or shape change here is a spine
change, not an implementation detail.

Per AD‑44 the wire **interface** is defined by **`event-contract.xml`**
(the source GJS parses natively and Python checks against); **`event-contract.json`**
keeps topics, payload schemas, and delivery semantics pinned to it. Both are
machine-checked by executable conformance; this prose is descriptive.

## Transport and ownership

- **Bus:** the per-user **session bus** (`DBUS_SESSION_BUS_ADDRESS`).
- **Well-known name (versionless):** `org.dotfiles.Events` — owned by **exactly
  one** process, the runtime daemon (`dotfiles-runtime daemon run`), the **hub**
  (AD‑38). Jobs/tools emit **through** the hub and never request an
  `org.dotfiles.*` name; if the daemon is absent they publish nothing.
- **Hub object path:** `/org/dotfiles/Events`, **interface (versioned):**
  `org.dotfiles.Events1`.
- **Job object path:** `/org/dotfiles/Job`, **interface (versioned):**
  `org.dotfiles.Job1` — served by **each job process on its own bus-attested
  unique name**. A job requests no well-known name. The hub is the **only**
  caller (see *Jobs and control*).

A breaking change becomes `org.dotfiles.Events2` (hub) or
`org.dotfiles.Job2` (job). **Additive** changes — a new method, signal,
topic, or optional field — stay on `…Events1`/`…Job1`; a **removed, retyped,
or newly-required** field is breaking. Versions may coexist on the bus under
distinct interface names while consumers migrate.

## Methods (hub)

| Method | Signature | Purpose |
| --- | --- | --- |
| `Emit` | `in: topic:s, payload:a{sv}` | publish a domain event |
| `BeginJob` | `in: kind:s, ttl:u` → `out: job_id:s` | register a job with a **lease**; hub allocates `job_id` |
| `RenewJob` | `in: job_id:s` | extend the lease |
| `AdoptJob` | `in: job_id:s, pid:u` | record the observed child PID (wrapper hand‑off) |
| `ReportProgress` | `in: job_id:s, fraction:d` | 0.0–1.0 |
| `EndJob` | `in: job_id:s, exit_code:i` | finish a job |
| `Control` | `in: job_id:s, action:s` | invoke a registered job action (pause/resume/stop) |
| `GetActiveJobs` | `out: jobs:a{ss}` | hydration: `job_id` → `kind` |
| `GetTopicState` | `in: topic:s` → `out: state:a{sv}` | hydration: last value + `_epoch`/`_seq` |

## Signals (hub is the sole emitter; every signal carries `epoch`)

| Signal | Members | Meaning |
| --- | --- | --- |
| `JobStarted` | `job_id:s, kind:s, epoch:u` | a job began |
| `JobProgress` | `job_id:s, fraction:d, epoch:u` | progress, 0.0–1.0 |
| `JobFinished` | `job_id:s, exit_code:i, epoch:u` | a job ended (0 = success; a lease expiry emits `exit_code = -1`) |
| `DomainEvent` | `topic:s, producer:s, seq:u, epoch:u, payload:a{sv}` | a tool-specific event |
| `JobsCleared` | `epoch:u` | the hub (re)started; all prior `job_id`s and topic state are invalid |

## Delivery, ordering, hydration

- Signals are **at-most-once**.
- `seq` is per-topic monotonic **within an `epoch`**; the `epoch` bumps on every
  hub start and resets `seq`. The hub emits **`JobsCleared(epoch)` on first start
  and every restart**; a consumer also treats D‑Bus `NameOwnerChanged` on
  `org.dotfiles.Events` as a restart signal (belt and braces, since
  `JobsCleared` is itself at‑most‑once).
- A consumer MUST **subscribe first, then hydrate** via `GetTopicState` /
  `GetActiveJobs`, and then **discard any signal whose `(epoch, seq)` is not
  greater than the hydrated pair**. `GetTopicState` returns reserved `_epoch: u`
  and `_seq: u` members; consumers compare `(epoch, seq)`, never `seq` alone.
- Consumers tolerate `JobsCleared` by re‑hydrating. A UI indicator is driven by
  that tool's **domain events**, never by `JobStarted`/`JobFinished` (AD‑37).

## Jobs and control

A job obtains a `job_id` via `BeginJob`, **renews its lease** while alive
(`RenewJob`), and ends with `EndJob`. A job that dies without ending is **expired
by the hub** (synthetic `JobFinished` with `exit_code = -1`). A wrapper that
spawns the observed process and exits **adopts** it (`AdoptJob`) so the hub
records the child.

### Control is request/response (hub is the single path)

A job that exposes control **serves** `/org/dotfiles/Job` / `org.dotfiles.Job1`
on the same connection it used for `BeginJob` (so its bus-attested unique name
is the one the hub saw). The allowed actions per job kind live in the hub
(`pause`/`resume`/`stop` for capture).

A consumer (bar/UI) calls **`Events1.Control(job_id, action)`** and nothing
else — it never addresses `Job1` and never shells the tool (AD‑38/H1). The hub:

1. validates `action` against the domain allowlist (unknown action →
   `NotControllable`; unknown/ended job → `UnknownJob`/`JobEnded`);
2. resolves the job's **bus-attested unique name**, recorded from the
   `BeginJob` sender (transport state only — never the domain);
3. makes a **synchronous outbound call** `org.dotfiles.Job1.Control(action)` to
   that unique name with a timeout, **outside** the dispatcher lock; and
4. returns success to the consumer **only on the job's ack**.

An unreachable, dead, or timed-out endpoint is a **loud** typed `UnknownJob`
(never a silent success — a UI can always tell the action did not apply). A
typed error the job returns crosses through unchanged. If the owner is gone,
the normal lease expiry still yields the synthetic `JobFinished(-1)`.

## Known topics and payload schemas

| Topic | Producer | Payload |
| --- | --- | --- |
| `icme.saved` | ICME (on save) | `{ "path": s }` |
| `capture.state` | capture controller | `{ "state": s (idle\|recording\|paused), "elapsed_seconds": x }` |
| `speedtest.state` | speed-test service | `{ "state": s (starting\|done\|error), "trigger": s (scheduled\|manual\|connect), "ssid": s?, "server_name": s?, "down_mbps": d?, "up_mbps": d?, "latency_ms": d?, "reason": s? }` |
| `clipboard.update` | clipboard controller | `{ "type": s (text\|image\|link\|code\|color\|emoji), "hash": s, "path": s, "preview": s }` |
| `clipboard.state` | clipboard controller | `{ "state": s (idle\|running\|paused), "job_id": s }` |
| `wallpaper.state` | wallpaper set | `{ "state": s (applying\|visible\|done\|error), "wallpaper_hash": s, "trigger": s (set\|regenerate\|reconcile\|reactive)? }` |

`clipboard.update` carries no binary: `path` is the cached image file for image
items (empty otherwise) and `preview` is a bounded text excerpt for text-like
items (empty for images). Consumers read image bytes from the path when they
render.

`clipboard.state` is emitted on every transition (start, pause, resume, stop)
so the overlay's incognito indicator reflects the daemon's real state and can
route `Control` to the hub-allocated `job_id`.

`wallpaper.state` is emitted by `dotfiles-runtime wallpaper set` on every
transition (`applying` at set start, `visible` after the swap,
`done`/`error` at finish) so the wallpaper selector can show progress
and refresh its LIVE badge. It is publish-only — the synchronous set
registers no job lease. `wallpaper_hash` is empty when the input never
validated. `visible` is additive: the new wallpaper is already on screen
while palette/effects/icons theming is still in flight — consumers treat
it as busy (no unlock, no crash; old consumers ignore it as an unknown
state). `done` still means the pipeline finished (UI may unlock);
`error` before `visible` means the swap failed (nothing changed on
screen), `error` after `visible` means theming failed with the new
wallpaper already live. `trigger` is an **optional/additive** discriminator
(old publishers remain valid and old consumers ignore it): it names the
operation that produced the transition — `set` (wallpaper set),
`regenerate` (icons regenerate), `reconcile` (standalone reconcile), or
`reactive` (daemon reactive converge).

`speedtest.state` supersedes the old terminal-only `speedtest.finished`. It is
emitted by the speed-test service on every transition — `starting` when a run
begins, then exactly one `done` or `error` when it settles, including when the
run's watchdog abandons it. `done` additionally carries the three measurement
doubles and the two context strings; `error` additionally carries a
single-line human-readable `reason` (never a traceback). `trigger` names the
operation that started the run: `scheduled` (the interval timer), `manual`
(the panel's Run now), or `connect` (the run-on-connect watcher).

`ssid` and `server_name` are the **context of a measurement**: which network
was up when the run started, and which server it measured against. They exist
so the notification card can name what it measured instead of pointing at the
panel — a card that says a speed test happened is not as useful as one that
says *what* it measured. Both MAY be empty strings when the information is
unavailable (no associated network, a backend that reports no server name), and
a consumer MUST fall back rather than render a blank. `ssid` is captured when
the run **starts**, not when it settles: the run measured the network that was
up at the time, so a link that changed mid-run would otherwise be mislabelled.

There is deliberately **no** progress fraction:
`speedtest-cli --json` returns one blob at the end, so the producer has no
intermediate value to publish and any determinate percentage a consumer drew
would be fabricated — consumers must render the in-flight run as indeterminate.
The service honours a user preference (`notify_on_test`) and emits nothing at
all when it is off; the measurement itself is still taken and stored.

The capture controller updates `capture.state` on every transition **and at
least once per second while recording**, so the bar renders the pushed value
(and may interpolate locally) — it derives **no** time from a file or a shared
wall clock, avoiding clock‑skew and suspend artifacts.

## Errors (typed)

`Emit`/job methods fail with typed D‑Bus errors: `UnknownTopic`,
`PayloadTooLarge`, `RateLimited`, `UnknownJob`, `JobEnded`, `NotControllable`.
A hub method call has a client‑side timeout; a caller treats a timeout as a
transient failure and re‑hydrates (never assumes success).

## Limits and validation

The hub validates every `Emit` **structurally** — schema, a maximum payload size
and nesting depth, and a publish rate — and rejects non‑conforming events. It
does **not** authenticate the sender: the session bus is a trusted same‑user
domain (AD‑38). Payloads are bounded, JSON‑compatible D‑Bus scalars and
containers, with no opaque binary. The hub never advertises a topic absent from
`event-contract.json`.

## Rules

- Producers never assume a consumer is present; consumers never assume a
  producer is running. Absence is a no-op, never an error.
- State is pushed, never polled.
