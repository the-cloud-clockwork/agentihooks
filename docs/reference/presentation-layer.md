---
title: Presentation layer architecture
parent: Reference
nav_order: 12
---

# Presentation layer architecture

The ledger pages, HOME and BIN form the presentation layer of agentihooks. Since 2026-10-06 the operator treats them as a product in their own right. This page records the constraints, the system as it runs today, what limits it, the target architecture, and the order of migration.

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
| Page load | The ledger server (`ledger_server.py`, stdlib `ThreadingHTTPServer`) reads `<slug>.html`, runs `ledger_core.sync`, and returns the whole page with the full ledger JSON seeded into `<script id="ledger-data">`. A busy swarm ledger is about 950 KB of HTML over a 6 MB JSON document. |
| Page rendering | `new_ledger.render` and `ledger_server.index_page` fill `__LEDGER_*__` / `__HOME_*__` placeholders in `template.html` / `home.html` with `re.sub`. `palette.css` and `tooltips.js` are inlined into every page. |
| Page code | `template.html` is about 2,700 lines with one inline script of about 180 functions: rendering, API calls, polling, layout and swarm control in one scope. |
| Operator write | The page sends `PUT /api/<slug>` with `{changes, ops}` and an `X-Ledger-Token`. The server replies with the whole document. |
| Agent write | The `agentihooks ledger` CLI sends the same `PUT` through the server. `watch_ledger` re-reads the JSON file from disk every 3 s. |
| Live refresh | Polling: the page fetches the whole document every 2 s and the swarm panel every 2 s, per open tab. There is no server push. |
| State | The JSON file per ledger is the record (with `_meta`: rev, path stamps, a bounded event log, recent seeds). A copy lives in the HTML seed. Swarm, inbox and health live in Redis. `localStorage` keeps only per-viewer folds, layout and caches. |
| Concurrency | One process, one global lock around read, reconcile, apply and write; atomic rename on write; optimistic merge by `_rev` against a window of past seeds. |

## What limits it

| Severity | Finding |
|---|---|
| P0 | Every op rewrites the whole document. One checkbox or chat line re-reads and re-writes megabytes of JSON and HTML under the global lock. |
| P0 | Whole-document polling. Each tab pulls the full document every 2 s and each watching agent re-reads the file every 3 s, so cost grows with ledger size, not with change. |
| P1 | One monolithic inline script with no module boundaries; behaviour cannot be tested in isolation from the page. |
| P1 | HTML built by placeholder substitution in Python, with escaping done by hand at each call site. |
| P1 | No API version and no schema: routes are string matches and bodies are validated by hand. |
| P2 | CSS and JS are inlined per page rather than served as cacheable files. |
| P2 | The ledger page response carries no Content-Security-Policy; only media and artifacts do. |
| P2 | One static bearer token per ledger, embedded in the page. Host and origin checks are the real CSRF defence. |
| P3 | A background thread scans every ledger HTML file every 2 s to catch hand edits. |

Not yet measured: writes per minute in a busy swarm, and lock wait under concurrent agents. These numbers decide how urgent the P0 items are.

## Target

```
 browser (static HTML + CSS + ES modules, no build)
   │  GET /app/*.html  /static/*.css  /static/js/*.js     (cacheable files)
   │  fetch /api/v1/...                                   (JSON resources)
   │  EventSource /api/v1/ledgers/{slug}/events           (server push)
   ▼
 ledger server  (Python stdlib HTTP, one process)
   ├─ API layer: routes → handlers → validation (JSON schema per resource)
   ├─ domain: ledger_core pure functions (apply_op, reconcile, validate)
   ├─ LedgerRepository (interface)
   │     ├─ FileLedgerRepository     (today's files, step 2)
   │     ├─ SqliteLedgerRepository   (WAL, step 3)
   │     └─ PostgresLedgerRepository (later, same interface)
   └─ swarm / inbox / health readers → Redis (unchanged)
```

### Front end

- Pages are plain HTML files with no Python templating: `ledger.html`, `home.html`, `bin.html`. Each page fetches its data from the API after load.
- `palette.css` plus one stylesheet per page, served as files. Colours stay in `palette.css`.
- JavaScript as native ES modules loaded with `<script type="module">`: `api.js` (fetch and errors), `events.js` (EventSource), `store.js` (in-page state), and one module per area (`tasks.js`, `phases.js`, `chat.js`, `swarm.js`, `outline.js`, `layout.js`). Repeated markup uses `<template>` elements.
- Impeccable live mode works on static HTML files with no build step, so this layout is its best case.

### API

- Versioned under `/api/v1`, one resource per path: `ledgers`, `ledgers/{slug}`, `…/phases/{id}`, `…/tasks/{id}`, `…/tasks/{id}/comments`, `…/questions`, `…/followups`, `…/notes`, `…/chat`, `…/artifacts`, `…/swarm` (control), `…/swarm/health`, `quota`, `bin`.
- `GET` returns one resource or a page of a collection; `POST`/`PATCH` change one thing and return it. No whole-document replies.
- A JSON schema per request body, checked in one place.
- `GET /api/v1/ledgers/{slug}/events?since=<rev>` is a server-sent event stream fed by the existing rev and event log. It replaces both pollers and the file re-read in `watch_ledger`.
- The CLI moves to the same endpoints, so agents and the page share one contract.

### Database

| Option | Fit today | Limit |
|---|---|---|
| **SQLite** (stdlib `sqlite3`, WAL) | One host, one server process, writes already serialized through that process. WAL lets readers run while one writer writes. Row updates make I/O proportional to the change. No new dependency or service. | Single host and single writer process. Running more than one server process, or moving into the cluster, brings the ceiling back. |
| Postgres (already runs in `anton-prod`) | Many processes, network access, mature tooling. | Adds a network dependency: the operator's local tool would depend on the cluster being up. |
| Redis as the record | Already carries swarm, inbox and health; a good bus for event fan-out. | Key-value shape fits phases → tasks → threaded comments → event log poorly. Keep it as transport, not as the record. |

**Decision: SQLite first, behind `LedgerRepository`.** A later move to Postgres means a second implementation of the same interface plus a one-time data copy.

Table sketch: `ledgers(slug, title, overview, size, rev, created_at, updated_at, closed_at)`, `phases(id, slug, title, description, depends_on, planning, done, position)`, `tasks(id, slug, phase_id, title, description, kind, lane, state, claimed_by, issue_url, pr_url, proof)`, `threads(id, slug, target, by, at, text, edited_at, deleted)` for comments, chat, notes and answers, `events(id, slug, rev, at, by, kind, target, payload)`, `seeds(slug, rev, doc)` for the merge window.

`LedgerRepository` methods follow what the server and CLI already do: `get_document`, `apply_ops`, `events_since`, `list_summaries`, `create`, `delete`, `restore`. The pure functions in `ledger_core` (`apply_op`, `apply_changes`, `reconcile_fields`, `reconcile_threads`, `validate`, `normalize`) stay as they are; only the file reads and writes move behind the interface.

## Migration (one PR per step, every step keeps today's pages working)

1. **Split the page script into ES modules.** Move the inline script of `template.html` into `static/js/*.js` loaded as modules; same placeholders, same API. Proof: the existing browser tests pass unchanged.
2. **Introduce `LedgerRepository` over the current files.** `FileLedgerRepository` wraps `load_state`, `sync` and `atomic_write`; the server and CLI call it. Proof: recorded requests return byte-identical responses before and after.
3. **Add `SqliteLedgerRepository` and an import script.** Choose the repository by environment variable. Run SQLite in shadow mode (write both, read files) and compare `get_document` and `events_since` on live ledgers before switching reads.
4. **Serve CSS and JS as files** with cache headers, and add a Content-Security-Policy to page responses.
5. **Add `/api/v1` resources** beside the old `PUT /api/<slug>`, one area at a time (tasks first), with JSON schemas.
6. **Add the event stream** and move the page and `watch_ledger` off polling.
7. **Make the pages static HTML** that load data from `/api/v1`; retire placeholder rendering and the HTML seed.
8. **Retire the old endpoints and the per-ledger JSON files** once nothing reads them.
