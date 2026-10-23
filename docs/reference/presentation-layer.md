---
title: Presentation layer architecture
parent: Reference
nav_order: 12
---

# Presentation layer architecture

The ledger pages, HOME and BIN form the presentation layer of agentihooks. Since 2026-10-06 the operator treats them as a product in their own right. This page records the constraints, the system as it runs today, what limits it, the target architecture, and the order of migration.

For heartbeat handling, reconnect examples and the live replay regression, see
[Ledger watcher replay verification](watch-ledger-replay.md).

## Constraints (operator decisions)

| Decision | Detail |
|---|---|
| Stack | Plain HTML, CSS and vanilla JavaScript. No frameworks (React, Next, Vue), no bundler, no build step. |
| API | A proper, versioned API layer between the pages and the store. |
| Database | A real database. Start on SQLite behind a storage interface so a later move (for example to Postgres) is a swap, not a rewrite. |
| Design | `DESIGN.md` at the repo root: design system 2026-001 structure, colours only in `palette.css`, no glow, near-black canvas. |

## Today

| Concern | How it works now |
|---|---|
| Page load | Every page is a static shell of about 22 KB: `shell.html` for a ledger, `home.html` for HOME and BIN. A shell carries the page version, slug and token in meta tags and links its CSS and JS under `/static/<page version>/`, served with an immutable cache; it holds no ledger record and no inline script or style. Serving it never writes the record. Every HTML response carries an enforced Content-Security-Policy: scripts, styles, images and connections from the same origin only. |
| Page rendering | The ledger page reads its metadata from `GET /api/v1/ledgers/{slug}` and its state from the event stream snapshot, then renders only what is visible: closed sections, folded comment dropdowns, the Contract and proof fold and hidden panels render nothing until opened, lists show 50 items per page, the operator notes list and question answers the latest 50, and every comment dropdown its newest eight ([Comment list paging](#comment-list-paging)). Proof bodies load from `GET /api/v1/ledgers/{slug}/tasks/{id}/workspace` when their fold opens. A deep link `#item-<list>-<id>` opens the section and page that hold its target. HOME and BIN render their rows from `GET /api/v1/ledgers` and `/api/v1/bin`. A ledger HTML file left from the old format is read only for its seed and token on import. |
| Page code | Native ES modules under `static/js/` for the ledger page and `static/home/home.js` for HOME and BIN; stylesheets under `static/css/` beside `palette.css`. |
| Operator write | The page sends `PUT /api/<slug>` with `{changes, ops}` and an `X-Ledger-Token`. The server answers with a bounded acknowledgment (applied and rejected ids, rev, warnings); the page takes the new state from the event stream. `GET /api/<slug>` is retired and answers 410. |
| Agent write | The `agentihooks ledger` CLI sends schema checked operations to `POST /api/v1/ledgers/{slug}/operations`. `agentihooks ledger --slug <slug> show` prints the whole ledger for an agent to read. `watch_ledger` follows the event stream: one snapshot, then patches. |
| Live refresh | Server push: each tab holds one `GET /api/v1/ledgers/{slug}/events` stream (fetch streaming, token in a header). It receives one snapshot of the ledger and the swarm status, then patches of them as they change, and a heartbeat every 5 s. The server samples swarm status once per ledger while a stream is open. Quota refresh keeps its own five minute cadence. |
| State | `ledgers.sqlite3` in the ledger folder is the record, behind `LedgerRepository`: one row per JSON value of each ledger (fields, collection items, threads), an append only event table, the page token and a summary row per ledger, and the bin and restore registries. No ledger JSON or HTML file is written. Hooks and gates read only the parts they need through a read only connection. Swarm, inbox and health live in Redis. `localStorage` keeps only per-viewer folds, layout and caches. |
| Concurrency | One `BEGIN IMMEDIATE` transaction per mutation applies the ops, derived priorities, notifications and alerts, the revision and its events, then writes only the rows whose values changed. WAL lets readers run beside the writer; the in process lock only orders the server's own threads. A generation number per ledger tells each process when its cached copy is stale. |

## Comment list paging

Every comment dropdown on the ledger page opens on its newest eight comments:
the comments under a task, a phase, a follow up, a question and the comments
on an operator note. A **Show N older comments · M left** control sits under
the list. Each click adds the next ten older comments above the ones already
shown; the last click shows what remains and the control disappears. The
summary line reads **Comments**, followed by **· N** once the item has
comments, where N counts every comment that is not deleted.

| Comments on the item | Shown on open | Control |
|---|---|---|
| 8 or fewer | All of them | None |
| 25 | Newest 8 | `Show 10 older comments · 17 left` |
| 25, after one click | Newest 18 | `Show 7 older comments · 7 left` |
| 25, after two clicks | All 25 | None |

The ledger server appends each new comment to the end of its thread, so
comments render oldest at the top and newest at the bottom, above the
**Add comment** button.

### Behaviour while the page is open

- The count of shown comments is kept per list. Opening more on one task
  leaves every other dropdown at eight.
- A comment arriving from the event stream keeps that count: the list still
  shows the same number of comments, now ending at the new one, and **M left**
  grows by one.
- Folding a dropdown and opening it again keeps its count. Reloading the page
  or opening it in another tab starts every list at eight again; only the
  open or folded choice of each dropdown is remembered per viewer.
- Deleted comments are not shown and are not counted.

### Show all comments and Hide all comments

The per section **Show all comments** and **Hide all comments** toggles, and
the page wide one, only open or fold the dropdowns. They do not change how
many comments each list shows: an opened dropdown still starts at its newest
eight.

### Other lists

The same pager drives every list on the page with a different page shape.
Sections and the outline show 50 items per page with **Show 50 more …** under
the list. The operator notes list and question answers show the latest 50
with **Show 50 older …** above the entries. A deep link `#item-<list>-<id>`
pages its target in.

| Shape | First page | Per click | Control |
|---|---|---|---|
| `COMMENT_PAGE` | 8 newest | 10 older | below the comments |
| `FULL_PAGE` (threads) | 50 newest | 50 older | above the entries |
| `FULL_PAGE` (lists) | 50 first | 50 next | below the items |

Both shapes live in `static/js/pages.js`. `commentsView` in
`static/js/threads.js` passes `COMMENT_PAGE` to `threadView`, so every
comment dropdown pages through one helper. The sizes are constants in the
page code, not settings: changing them is a code change through a pull request.

### Troubleshooting

| Report | Cause and check |
|---|---|
| A dropdown shows eight comments while its summary counts more | Expected: press **Show N older comments** under the list. |
| The control vanished before the oldest comment | It disappears only when every comment that is not deleted is shown. Compare the summary count with the comments shown; deleted comments are not counted. |
| A list went back to eight comments | The page was reloaded or opened in a new tab, where every list starts at eight. |
| **Show all comments** did not show older comments | That toggle only opens the dropdowns; use **Show N older comments** inside each one. |

## Write thread and stream publication

An operations write (`POST /api/v1/ledgers/{slug}/operations`, and the page's
`PUT /api/<slug>`) does only the work its reply needs on the request thread:
copy the stored ledger, apply the operations, write the changed rows and answer.
Building the page stream view and diffing it against the last frame happens on
one background thread. Delivered by
[pull request 5136](https://github.com/the-cloud-clockwork/agentihooks/pull/5136).

Before that change, a profile of the recorded operations corpus (issue 4980)
showed each write doing work proportional to the whole ledger four times on the
request thread: the stream view rebuild and diff (32 percent of write CPU), the
stored fragment rebuild (33), a whole document encode and parse to get a
mutable copy (19) and the mutation itself (12). The change removes the first and
the third from the request thread; the fragment rebuild and the mutation stay.

### Stream publication

The server's repository is wrapped by `Publishing`, which hands each new stored
generation of a ledger to the stream publisher:

| Step | What happens |
|---|---|
| Write or read | `apply_ops`, and any read through `current`, compares the stored generation with the last one published for that ledger. A newer one is queued and the caller returns at once. |
| Queue | One entry per ledger. A newer generation replaces an older one still waiting, so a burst of writes publishes only the newest state. |
| Thread | One daemon thread named `ledger-publish`, started on the first queued generation, publishes each queued generation to every stream publisher and records it as published. |
| Failure | The write has already returned its reply. The thread writes `stream publish for <slug> generation <n>: <error>` to the server's stderr and the stream keeps its last frame. The next read or write of that ledger queues the generation again. |

Acceptance, refusal, revision, persistence and relay outcomes of a write do not
depend on the publisher. One outcome changed on purpose: a publication failure
used to raise into the writer after the write had committed; now the writer
never sees it.

Stream subscribers therefore see a write as a patch shortly after its reply,
not before it. A client that needs the result of its own write reads the reply,
which carries the applied and rejected ids and the revision.

### Mutable copy

A write needs a copy of the stored state that the operations may change. It
used to get one by encoding the whole ledger to JSON text and parsing it back.
`Entry.mutable()` now copies the state structurally with the same result a JSON
round trip gives:

| Value | Copy |
|---|---|
| list | A new list of copied items |
| dict whose keys are all strings | A new dict of copied values |
| `str`, `int`, `float`, `bool`, `None` | Shared, since they cannot change |
| Anything else: a tuple, a dict with a non string key, a subclass | Encoded and parsed on its own, as before |

`apply_ops` copies the stored entry this way before applying operations, and
copies the written entry again for the caller unless it passes
`copy_state=False`. On the recorded 8.2 MB corpus the structural copy took 34 ms
of thread CPU against 79 ms for encode and parse.

### Measuring operations writes

The live server records CPU and wall time of every write in its
[request metrics](ledger-request-metrics.md) journal. Operations write CPU
median and wall p95 over one fixed hour:

```bash
END=$(date +%s)
python -m scripts.swarm_ledger.request_metrics --writes --hours 1 --end "$END" --limit 10000 \
  | jq -c '{matched, truncated, complete: .coverage.complete},
      (.routes[] | select(.route == "/api/v1/ledgers/:slug/operations") | {method, count,
        cpu_median: ([.records[].cpu_seconds] | sort | .[(length/2|floor)]),
        wall_p95: ([.records[].wall_seconds] | sort | .[(length*0.95|ceil)-1])})'
```

A read of the live server on 23 October:

```json
{"matched":154,"truncated":false,"complete":true}
{"method":"POST","count":96,"cpu_median":0.31858792599999997,"wall_p95":3.658822054974735}
```

The targets set for this change were a CPU median below 0.433 s and a wall p95
below 1.37 s, compared over equal hours before and after the server restarted on
the change. CPU is time on the request thread only, so publication work no
longer counts toward it; wall time also includes lock wait and the time other
threads hold the CPU, which the `lock_wait_seconds` field separates.

In CI, the `operations-cpu` proof replays the recorded corpus on the base and on
the head, on one runner, and compares every outcome, CPU per write and peak
memory:

```bash
gh workflow run proofs.yml --ref <branch> -f proof=operations-cpu -f base=origin/dev
```

The replay waits for queued publications (`Publishing.drain`) before it reads
the stream, so stream outputs compare exactly. On pull request 5136 the outputs
were identical, CPU per write fell from a mean of 0.247 s to 0.200 s and a p95 of
0.351 s to 0.263 s, and peak memory stayed at 313 MB
([run 39375266112](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39375266112)).
That job stays red on a fixed halving bar set by an earlier task, which needs the
fragment rebuild gone too.

`tests/swarm_ledger/test_writer_thread_work.py` guards the behaviour: it
profiles the request thread during a replay and fails if whole ledger encoding
runs there or if the stream publisher runs on the writer's own thread. It read
red on the code before the change
([run 39375053092](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39375053092)).
The [ledger load gate](ledger-load-gate.md) stayed green
([run 39375289330](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39375289330)).

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| A write returned but the page did not change | The publication is still queued or failed. Look for `stream publish for <slug> generation <n>` on the server's stderr; the next read or write of that ledger retries. A failure on every retry is a bug in the stream view, not in the write. |
| A test reads the stream right after a write and sees the old frame | Publication runs on another thread. Call `drain(timeout)` on the repository first; it raises `TimeoutError` when publications are still pending after the timeout. |
| Operations CPU median stayed near 0.43 s | The server still runs code from before pull request 5136. Restart it on current code and read a fresh hour. |
| Wall p95 high while CPU median is low | The time is spent outside the request thread's CPU: read `lock_wait_seconds` and `other_wall_seconds` in the [request metrics](ledger-request-metrics.md#summary-view) before blaming the write path. |

## What limits it

| Severity | Finding |
|---|---|
| Resolved | Every op rewrote the whole document. On a copy of the largest live ledger one chat line wrote 16.6 MB and took 0.73 s; now it writes about 150 KB of database pages and takes 0.09 s, and twenty writes from four threads wait 0.41 s at the median instead of 1.90 s. |
| Resolved | Each operations write built the page stream view and encoded the whole ledger on the request thread. Both now stay off it ([Write thread and stream publication](#write-thread-and-stream-publication)). |
| Resolved | Whole-document polling, replaced by the event stream: idle tabs and watchers transfer only heartbeats, and a change sends a patch of the changed items. |
| P1 | One monolithic inline script with no module boundaries; behaviour cannot be tested in isolation from the page. |
| P1 | HTML built by placeholder substitution in Python, with escaping done by hand at each call site. |
| P1 | No API version and no schema: routes are string matches and bodies are validated by hand. |
| P2 | CSS and JS are inlined per page rather than served as cacheable files. |
| P2 | The ledger page response carries no Content-Security-Policy; only media and artifacts do. |
| P2 | One static bearer token per ledger, embedded in the page. Host and origin checks are the real CSRF defence. |
| Resolved | The 2 s scan of every ledger HTML file and the merge of hand edited seeds are gone; agents change the ledger through the CLI. |

Not yet measured: writes per minute in a busy swarm.

## Target

```
 browser (static HTML + CSS + ES modules, no build)
   │  GET /app/*.html  /static/*.css  /static/js/*.js     (cacheable files)
   │  fetch /api/v1/...                                   (JSON resources)
   │  fetch stream /api/v1/ledgers/{slug}/events          (server push)
   ▼
 ledger server  (Python stdlib HTTP, one process)
   ├─ API layer: routes → handlers → validation (JSON schema per resource)
   ├─ domain: ledger_core pure functions (apply_op, apply_changes, validate)
   ├─ LedgerRepository (interface)
   │     ├─ SQLiteLedgerRepository   (WAL, the record)
   │     └─ PostgresLedgerRepository (later, same interface)
   └─ swarm / inbox / health readers → Redis (unchanged)
```

### Front end

- Pages are plain HTML files with no Python templating: `ledger.html`, `home.html`, `bin.html`. Each page fetches its data from the API after load.
- `palette.css` plus one stylesheet per page, served as files. Colours stay in `palette.css`.
- JavaScript as native ES modules loaded with `<script type="module">`: `api.js` (fetch and errors), `events.js` (event stream over fetch), `store.js` (in-page state), and one module per area (`tasks.js`, `phases.js`, `chat.js`, `swarm.js`, `outline.js`, `layout.js`). Repeated markup uses `<template>` elements.
- Impeccable live mode works on static HTML files with no build step, so this layout is its best case.

### API

- Versioned under `/api/v1`, one resource per path: `ledgers`, `ledgers/{slug}`, `…/phases/{id}`, `…/tasks/{id}`, `…/tasks/{id}/comments`, `…/questions`, `…/followups`, `…/notes`, `…/chat`, `…/artifacts`, `…/swarm` (control), `…/swarm/health`, `quota`, `bin`.
- `GET` returns one resource or a page of a collection; `POST`/`PATCH` change one thing and return it. No whole-document replies.
- A JSON schema per request body, checked in one place.
- `GET /api/v1/ledgers/{slug}/events` with `Accept: text/event-stream` is a server-sent event stream; without that header the same route stays the paginated events collection. Each event carries an opaque cursor as its id; a reconnect sends `Last-Event-ID` and gets the retained events after it (256 per ledger). A cursor from another server start, another ledger or past retention answers 410 `cursor_expired`, and the client reconnects without it to get a fresh snapshot. It replaces both pollers and the file re-read in `watch_ledger`.
- The CLI moves to the same endpoints, so agents and the page share one contract.

### Database

| Option | Fit today | Limit |
|---|---|---|
| **SQLite** (stdlib `sqlite3`, WAL) | One host, one server process, writes already serialized through that process. WAL lets readers run while one writer writes. Row updates make I/O proportional to the change. No new dependency or service. | Single host and single writer process. Running more than one server process, or moving into the cluster, brings the ceiling back. |
| Postgres (already runs in `anton-prod`) | Many processes, network access, mature tooling. | Adds a network dependency: the operator's local tool would depend on the cluster being up. |
| Redis as the record | Already carries swarm, inbox and health; a good bus for event fan-out. | Key-value shape fits phases → tasks → threaded comments → event log poorly. Keep it as transport, not as the record. |

**Decision: SQLite first, behind `LedgerRepository`.** A later move to Postgres means a second implementation of the same interface plus a one-time data copy.

Tables: `ledgers(slug, revision, generation, token, summary, touched_at)`; `fields`, `resources` and `threads(slug, path, parent, key, position, kind, value)`, one row per JSON value, where `resources` holds collection items and `threads` holds comments and answers; `events(slug, key, revision, position, value)`; `registry(slug, path, value)` for the bin and restore marks; `revisions`, `seed_base` and `seed_deltas` keep the seeds an imported ledger carried, read only by an explicit export.

`LedgerRepository` methods: `get_document`, `read` (named parts only), `apply_ops`, `events_since`, `list_summaries`, `exists`, `token`, `create`, `delete`, `restore`, `export_document`, `import_document`. `agentihooks ledger storage export SLUG` and `import PATH` move a complete document in and out losslessly; an imported document must export equal to its source or nothing is stored. A ledger JSON or HTML file found in the ledger folder is imported once: it is first copied into `.imported/` and the copy checked byte for byte, then imported and verified, then removed. `agentihooks ledger storage cutover` does that for the whole folder.

## Migration (one PR per step, every step keeps today's pages working)

1. **Split the page script into ES modules.** Move the inline script of `template.html` into `static/js/*.js` loaded as modules; same placeholders, same API. Proof: the existing browser tests pass unchanged.
2. **Introduce `LedgerRepository` over the current files.** Done 2026-10-07.
3. **Add `SqliteLedgerRepository` and an import script.** Done 2026-10-07 as a shadow; the shadow is retired by step 8.
4. **Serve CSS and JS as files** with cache headers, and add a Content-Security-Policy to page responses. Done 2026-10-07.
5. **Add `/api/v1` resources** beside the old `PUT /api/<slug>`, one area at a time (tasks first), with JSON schemas. Done 2026-10-07.
6. **Add the event stream** and move the page and `watch_ledger` off polling. Done 2026-10-07.
7. **Make the pages static HTML** that load data from `/api/v1`; retire placeholder rendering and the HTML seed. Done 2026-10-07.
8. **Retire the old endpoints and the per-ledger JSON files** once nothing reads them. Done 2026-10-08: SQLite is the record, every hook, gate, swarm and CLI reader goes through the repository, the whole document `GET /api/<slug>` is gone and the page write route answers with a bounded acknowledgment.
