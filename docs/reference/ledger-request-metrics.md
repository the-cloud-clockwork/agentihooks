---
title: Ledger request metrics
parent: Reference
nav_order: 28
---

# Ledger request metrics

The ledger server times every request it answers: wall time, CPU time on the
request thread and time spent waiting for a ledger lock. The request metrics
command reads those timings back in two views. The summary view gives write
percentiles and CPU per route. The writes view returns every individual write in
a fixed window, grouped by method and route, so a rise in write latency can be
attributed to the routes that caused it.

The server journals its timings beside its ledgers and restores them when it
reloads or restarts, so a reload no longer erases the records of the hour before
it. When the server cannot account for a span, the reply names it.

Delivered by [PR #4331](https://github.com/the-cloud-clockwork/agentihooks/pull/4331).
Timing does not change what a write does.

## Reading timings

```bash
python -m scripts.swarm_ledger.request_metrics [--hours H] [--end EPOCH] [--writes] [--limit N] [--url URL]
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--hours` | `24` | Window length in hours. Must be above 0 and at most 168, the seven day retention |
| `--end` | now | Window end as a Unix epoch time in seconds. Fixes the window in both views, so two reads of the same past hour agree |
| `--writes` | off | Return individual write records instead of the summary |
| `--limit` | `1000` | Most write records returned, 1 to 10000. Writes view only |
| `--url` | the ledger server address | Server base URL. Without it the command uses the same address every ledger client uses: a routed or hosted ledger first, then `SWARM_URL` or `LEDGER_URL` for a remote client, then `LEDGER_HOST` and `LEDGER_PORT` (default `127.0.0.1:8765`) |

The command prints one JSON object on one line with sorted keys. An invalid flag
value exits before any request with a usage error such as
`hours must be positive and at most seven days`.

The command is a thin client of the HTTP endpoint, which any caller can read
directly:

```bash
curl -s 'http://127.0.0.1:8765/api/v1/metrics?hours=1'
curl -s 'http://127.0.0.1:8765/api/v1/metrics?view=writes&hours=1&end=1792653900&limit=500'
```

| Query parameter | Matches flag |
|-----------------|--------------|
| `hours` | `--hours` |
| `end` | `--end` |
| `view` | `summary` (default) or `writes`; `--writes` sends `writes` |
| `limit` | `--limit`; read only when `view=writes` |

The endpoint needs no ledger token. It applies the server's usual host and
origin checks and answers `403` to a request that fails them.

### Writes view

A write is any `POST`, `PUT`, `PATCH` or `DELETE` request. Exports are `POST`
requests and appear as writes on their own route. Every other method is left out
of this view.

```json
{
  "window_start": 1792653880.86,
  "window_end": 1792654060.86,
  "coverage": {"complete": true, "started_at": 1792652916.74, "held_since": 1792620631.27,
               "lost": [], "evicted_through": null, "capacity": 50000},
  "matched": 5,
  "truncated": true,
  "routes": [
    {"method": "POST", "route": "/api/v1/ledgers/:slug/export", "count": 2, "records": [
      {"ended": 1792653927.83, "wall_seconds": 1.398, "cpu_seconds": 0.938, "lock_wait_seconds": 0.0},
      {"ended": 1792653956.92, "wall_seconds": 1.246, "cpu_seconds": 0.980, "lock_wait_seconds": 0.0}]},
    {"method": "POST", "route": "/api/v1/ledgers/:slug/operations", "count": 1, "records": [
      {"ended": 1792653958.38, "wall_seconds": 0.323, "cpu_seconds": 0.217, "lock_wait_seconds": 0.012}]}
  ]
}
```

This reply was captured from the live server with `--writes --hours 0.05 --limit 3`
and shortened to three decimals.

| Field | Meaning |
|-------|---------|
| `window_start`, `window_end` | The window read, as epoch seconds |
| `matched` | Writes held inside the window |
| `truncated` | `true` when `matched` exceeds `limit`. The newest `limit` writes are returned and the oldest are dropped |
| `routes[].method`, `routes[].route` | The request method and its route template. Slugs and item ids are replaced with `:slug`, `:item` and `:resource`, so every ledger's writes to one route group together |
| `routes[].count` | Records returned for that route, after the limit |
| `records[].ended` | When the request finished, epoch seconds |
| `records[].wall_seconds` | Elapsed time for the whole request |
| `records[].cpu_seconds` | CPU time of the request thread |
| `records[].lock_wait_seconds` | Time spent waiting to acquire ledger locks and to begin a database transaction |

Wall time minus CPU time minus lock wait is the remainder: input and output,
waiting for other threads, and anything else the request did off CPU.

### Summary view

The summary covers every request in the window, writes and reads.

| Field | Meaning |
|-------|---------|
| `writes.count` | Writes in the window |
| `writes.p95_wall_seconds` | 95th percentile write wall time, or `null` with no writes |
| `writes.cpu_seconds`, `writes.lock_wait_seconds` | Totals across those writes |
| `writes.other_wall_seconds` | Total remainder, wall minus CPU minus lock wait, per write and never below zero |
| `slow_days` | Per UTC day, the writes that took 5 seconds or more |
| `routes` | Per method and route, the request count and total CPU seconds, busiest CPU first |
| `coverage` | As in the writes view |

The summary's write percentile mixes every write route. Use the writes view to
find which route moved it.

## Coverage

Every reply carries `coverage`, which states whether the server holds every
request of the window.

| Field | Meaning |
|-------|---------|
| `complete` | `true` only when `held_since` is at or before the window start, `evicted_through` is before the window start and no lost span overlaps the window |
| `started_at` | When the current server process started |
| `held_since` | The earliest time the server holds records from. After a restore it is the start of the oldest journal still within retention, never older than seven days |
| `lost` | Spans, as `{"start": …, "end": …}`, that overlap the window and whose requests were never journaled |
| `evicted_through` | The newest record dropped from the memory ring, or `null` when none was dropped. Records at or before it may be missing |
| `capacity` | Records the memory ring holds, 50000 |

The server keeps at most 50000 records in memory and drops records older than
seven days. A busy server can therefore hold less than seven days: when the ring
is full, each new record pushes out the oldest and moves `evicted_through`
forward. Read `complete` before comparing two windows; a window with
`complete: false` undercounts.

## Journal across reloads

The server journals its timings to `.request-metrics.jsonl` in the ledger folder,
`$LEDGER_DIR` (default `~/development-ledger`). Each line is one JSON array.

| Line | Written when |
|------|--------------|
| `["held", t]` | A restore or full rewrite records the earliest time held |
| `["lost", start, end]` | A restore records a span the previous process never journaled |
| `["evicted", t]` | The memory ring dropped a record, so the mark survives a restart |
| `["start", t]` | A restore or full rewrite records the process start |
| `["s", ended, method, route, wall, cpu, lock_wait]` | One request timing |
| `["flush", t]` | Each pass of the server's watch loop, about every two seconds, after appending new timings |
| `["stop", t]` | A code reload or shutdown, after the last timings |

The lifecycle:

1. On start the server reads the journal, keeps records and lost spans within
   the seven day retention, and rewrites the journal atomically through a
   temporary file in the same folder.
2. Each watch loop pass appends the requests timed since the last pass and a
   flush mark. When the appended lines would exceed the ring capacity, the pass
   rewrites the whole journal from memory instead.
3. A code reload (`SWARM_RELOAD=1`, the default for a swarm started server) and a
   normal shutdown write a stop mark before the process exits.
4. A journal that ends without a stop mark means the previous process died. The
   span from its last flush or record to the new process start is recorded as
   lost and reported in `coverage.lost`.

A journal write that fails, the restore rewrite included, prints
`request metrics journal: <error>` to the server's stderr and forces a full
rewrite on the next pass. The server keeps serving requests through any journal
error. Lines that are not valid JSON or have the wrong shape are skipped on
restore.

The journal needs no configuration. Its location follows `LEDGER_DIR`; its
retention and capacity are fixed in code.

### Limits

- A request still in flight when a reload executes is not journaled and is not
  reported as lost.
- A crash while the ledger folder refuses every journal write leaves that span
  unnamed: no stop mark and no flush mark mean the next start cannot place it.
- After a crash, requests timed since the last flush were never journaled. They
  fall inside the reported lost span rather than being held.

## Examples

Per route write p95 and lock wait over the last hour:

```bash
python -m scripts.swarm_ledger.request_metrics --writes --hours 1 --limit 10000 \
  | jq -c '[.routes[] | {method, route, count,
      p95: ([.records[].wall_seconds] | sort | .[(length*0.95|ceil)-1]),
      lock_wait: ([.records[].lock_wait_seconds] | add)}] | sort_by(-.count) | .[:3][]'
```

```json
{"method":"POST","route":"/api/v1/ledgers/:slug/operations","count":138,"p95":6.155,"lock_wait":11.019}
{"method":"POST","route":"/api/v1/ledgers/:slug/export","count":43,"p95":4.042,"lock_wait":0}
```

Compare the same fixed hour before and after a change. Fix `--end` so later
reads return the same window, and check that the reply holds all of it:

```bash
END=$(( $(date +%s) - 7200 ))
python -m scripts.swarm_ledger.request_metrics --writes --hours 1 --end "$END" --limit 10000 \
  | jq -c '{matched, truncated, complete: .coverage.complete, lost: .coverage.lost}'
```

```json
{"matched":65,"truncated":false,"complete":true,"lost":[]}
```

Keep the printed `END` value and pass it again after the change; the window
stays the same hour as long as it is within the seven day retention.

A `truncated: true` reply needs a shorter `--hours` or a higher `--limit`.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `400` with `{"error": {"code": "invalid_window", "message": "hours out of range"}}` | Any rejected parameter returns this body: `hours` outside 0 to 168, a non finite or non numeric `hours` or `end`, a `view` other than `summary` or `writes`, or a `limit` outside 1 to 10000 |
| `403 host not allowed` or `403 origin not allowed` | The request came through a host name or browser origin the server does not accept. Read from the server's own address |
| `complete: false` with a non empty `lost` | The previous server process stopped without a stop mark inside the window. Those requests were not journaled and cannot be recovered |
| `complete: false` with `held_since` after the window start | The server holds nothing that old: the window starts before the seven day retention, or before the first journal |
| `complete: false` with `evicted_through` at or after the window start | The 50000 record ring was full and dropped records in the window. Read a newer window |
| `request metrics journal: …` on the server's stderr | The ledger folder refused a journal write. Fix the folder's permissions or free space; the next pass rewrites the whole journal |
| No `.request-metrics.jsonl` in the ledger folder | The server has not started on code from PR #4331, or it cannot write the folder. Check the stderr line above |

Related: [Ledger load gate](ledger-load-gate.md) grades write latency on a
scratch server in CI; this page reads the live server.
