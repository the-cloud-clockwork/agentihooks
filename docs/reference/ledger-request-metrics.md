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

Delivered by [pull request 4331](https://github.com/the-cloud-clockwork/agentihooks/pull/4331).
Timing does not change what a write does.

## Command-line write baselines

`agentihooks ledger time-writes` measures the comment writes an agent makes from
the command line. Each write runs in a fresh process, one after another, using
the timer's Python interpreter and `python -m scripts.install api`. It includes
process startup and the ledger copy work a fresh agent command pays for.
Pull request [13144](https://github.com/the-cloud-clockwork/agentihooks/pull/13144)
added this command and command-line request timing.

These are real writes to an existing task, using the fixed comment text
`Timing one agent ledger write`. Choose an existing scratch ledger and task,
and an identity permitted to comment there. The command creates neither the
ledger nor the task, and leaves its comments in place. It applies the same
identity checks, revision guards and write gates as an ordinary comment.

```bash
agentihooks ledger time-writes \
  --slug "$TIMING_SLUG" \
  --as "$TIMING_AGENT" \
  --count 5 \
  "$TIMING_TASK"
```

Set `TIMING_SLUG`, `TIMING_AGENT` and `TIMING_TASK` to the scratch ledger slug,
your agent name and the task id before running the examples. Pass the task id
alone: the command adds `tasks/` itself.

| Argument | Default | Meaning |
|----------|---------|---------|
| `--slug` | required | Existing ledger slug |
| `--as` | required | Identity making every comment write |
| `--count` | `5` | Integer number of sequential writes; use a positive count for a baseline |
| `task` | required | Bare task id within that ledger |

The comment text is fixed; there is no text option. A count of zero or less
performs no writes and prints empty `writes` and `summary` values.

### Store configuration and saved results

The child processes inherit the timer's environment and use the configured
store directly. With `AGENTIHOOKS_SWARM_DATABASE_URL` set they use Postgres;
otherwise they use SQLite under `LEDGER_DIR` (default
`~/development-ledger`). A ledger server is unnecessary, and this command has
no server URL option. See [Ledger write path](ledger-write-path.md#the-store-a-write-lands-in)
and [Shared ledger database sessions](shared-ledger-database.md).

For an existing scratch ledger in Postgres, pass its database setting by
environment reference and save the report:

```bash
TIMING_OUTPUT_DIR="$HOME/scratchpad/ledger-timing"
mkdir -p "$TIMING_OUTPUT_DIR"
export AGENTIHOOKS_SWARM_DATABASE_URL="$SCRATCH_LEDGER_DATABASE_URL"

agentihooks api \
  --slug "$TIMING_SLUG" \
  --as "$TIMING_AGENT" \
  join

agentihooks ledger time-writes \
  --slug "$TIMING_SLUG" \
  --as "$TIMING_AGENT" \
  --count 5 \
  "$TIMING_TASK" > "$TIMING_OUTPUT_DIR/baseline.json"
```

Here `SCRATCH_LEDGER_DATABASE_URL` must already hold the scratch database
setting. Joining uses the identity that will make the comments; a pinned agent
must use its own name. For an existing SQLite scratch store, explicitly clear
the database URL for the command and point it at that store's folder:

```bash
AGENTIHOOKS_SWARM_DATABASE_URL="" \
LEDGER_DIR="$SCRATCH_LEDGER_DIR" \
agentihooks ledger time-writes \
  --slug "$TIMING_SLUG" \
  --as "$TIMING_AGENT" \
  --count 5 \
  "$TIMING_TASK" > "$TIMING_OUTPUT_DIR/sqlite-baseline.json"
```

Set `SCRATCH_LEDGER_DIR` to the existing scratch store, and join there with the
same store settings if needed. Keep the report with the UTC measurement date,
code revision, host and store used. Compare runs with the same count and
comparable ledger sizes and host load; a small scratch ledger does not measure
the copy cost of a much larger ledger.

### Read the baseline report

A successful run prints one indented JSON object. Child comment replies are
suppressed; child errors remain on stderr. Duration figures are seconds.

| Field | Meaning |
|-------|---------|
| `slug`, `task`, `count` | Target and requested write count |
| `load_before`, `load_after` | Host load averages over one, five and fifteen minutes, before and after the run |
| `writes` | One measurement object per successful child process, in execution order |
| `writes[].process_wall`, `process_cpu` | Entire child elapsed time and combined child user and system CPU time |
| `writes[].requests` | Number of measured in-process requests made by that child |
| `writes[].request_wall`, `request_cpu` | Sum of all its request wall and thread CPU times |
| `writes[].write_wall`, `write_cpu` | Sum of its `POST` request wall and thread CPU times |
| `writes[].lock_wait`, `postgres_wait` | Sum of the recorded ledger lock and Postgres waits across its requests |
| `writes[].<step>_wall`, `<step>_cpu` | Sum of wall and thread CPU time for each observed substep |
| `summary.<figure>.median`, `.max` | Median and maximum across writes for each observed figure, including the request count |

A substep absent from one write counts as zero in that write's summary. A
substep absent from every write has no summary entry. Raw request records also
carry `client_wait`; the baseline report does not aggregate that field.

For example, extract the process figures from a saved run:

```bash
jq '{count, process_wall: .summary.process_wall,
     process_cpu: .summary.process_cpu}' "$TIMING_OUTPUT_DIR/baseline.json"
```

Illustrative output only; these numbers are not a measured current baseline:

```json
{
  "count": 5,
  "process_wall": {"median": 1.2, "max": 1.8},
  "process_cpu": {"median": 0.8, "max": 0.9}
}
```

### Interpret the substeps

Each observed name appears with both `_wall` and `_cpu` suffixes:

| Substep | Work measured |
|---------|---------------|
| `copy_adopt` | Attempt to adopt the host's saved ledger copy |
| `copy_refresh` | Refresh the copy to the stored generation |
| `copy_load` | Load the whole ledger when a refreshed copy is unavailable |
| `copy_document` | Build the document fragments, remember the entry and keep its copy |
| `guard_read` | Read the current document before checking and applying operations |
| `write_gate` | Obtain the write fence and construct its guarded operations |
| `apply_locked` | Read the entry, apply the mutation and write it while holding the ledger lock |
| `publish` | Publish the fence result, relay inbox events, process the Doctor phrase and deliver alerts |
| `hierarchy_sync`, `outbox_write`, `refresh_record`, `event_append` | The four transaction substeps described in the server writes view below |

These spans nest. `apply_locked` contains the four transaction substeps and can
also contain copy work on a cold entry; copy work otherwise occurs inside
`guard_read`. Adding enclosing and enclosed times double-counts the same work.
Lock acquisition precedes `apply_locked`; use the wait figures to inspect it.

Process CPU and request CPU have different scopes: the former includes process
user and system usage, while request and substep CPU measure the executing
thread. Process wall time also includes startup and work outside the measured
requests. Fresh processes can still adopt saved host copies, so a fresh process
does not guarantee a whole-ledger rebuild. This command measures sequential
comment writes, without a latency threshold or a concurrent-load scenario.

### Raw command-line request timings

For an ordinary command, set `AGENTIHOOKS_LEDGER_TIMING_FILE` to a writable file
to append one JSON object per in-process ledger request:

```bash
AGENTIHOOKS_LEDGER_TIMING_FILE="$TIMING_OUTPUT_DIR/requests.jsonl" \
agentihooks api \
  --slug "$TIMING_SLUG" \
  --as "$TIMING_AGENT" \
  comment "tasks/$TIMING_TASK" "Timing one agent ledger write"

jq '{method, route, wall, cpu, lock_wait, client_wait,
     postgres_wait, steps}' "$TIMING_OUTPUT_DIR/requests.jsonl"
```

Create the parent directory first. Records append to the named file; an
ordinary comment can make multiple requests, each leaving its own line. Each
record contains `method`, normalized `route`, diagnostic `detail`, `wall`,
`cpu`, `lock_wait`, `client_wait`, `postgres_wait` and `steps`. The `steps`
object maps each observed name to `{"wall": seconds, "cpu": seconds}`. All
times are seconds, and request and substep CPU use the thread clock.

With the variable unset or empty, command-line requests write no timing file.
Inside an already measured server request, their substeps join that request
without appending another command timing line. Server write records retain
their existing four transaction substep columns.

The baseline command supplies a separate private timing file to each child,
overriding any inherited `AGENTIHOOKS_LEDGER_TIMING_FILE` for that child. It
removes those files when the run ends. Save stdout to retain the baseline;
use the ordinary command example when you need persistent raw request lines.

### Baseline failures

The first child that exits nonzero ends the run with that exit code and a
message such as `write 2 of 5 exited 3` on stderr. No final JSON report is
printed. Earlier writes remain committed; a failed timing-file append can also
occur after the underlying write succeeded. Inspect the task before retrying.

| Symptom | Check |
|---------|-------|
| Usage error or missing ledger or task | Use the required slug and identity flags, an existing scratch task and its bare id; passing `tasks/` makes the command add it twice |
| Identity refused | Use the pinned agent's own name and a permitted identity on the target ledger; see [Who a call acts as](ledger-write-path.md#who-a-call-acts-as) |
| File permission or missing directory error | Create the report or timing file's parent directory and check it is writable; raw timing output does not create parent directories |
| No final JSON | Read the child's stderr and exit code; a failed child stops the run immediately |
| Process timings exist but request timings are empty | Verify the installed code includes command-line request timing and that the child can write its private timing file; the timer runs children with its own interpreter |
| A copy substep is missing | It was not observed on that path; compare the copy substeps together and the saved-copy state before concluding a whole rebuild happened |

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
| `records[].hierarchy_sync_seconds` | Time the write spent syncing the ledger hierarchy rows |
| `records[].outbox_write_seconds` | Time the write spent writing its outbox events |
| `records[].refresh_record_seconds` | Time the write spent recording the changed paths readers refresh from |
| `records[].event_append_seconds` | Time the write spent appending and trimming ledger events |

Wall time minus CPU time minus lock wait is the remainder: input and output,
waiting for other threads, and anything else the request did off CPU.

The four substep times are wall time spans inside the write transaction. They
overlap CPU time and database waits, so they are not subtracted from the
remainder; they name which part of the write the time went to. A request that
writes no ledger reports them as zero.

### Summary view

The summary covers every request in the window, writes and reads.

| Field | Meaning |
|-------|---------|
| `writes.count` | Writes in the window |
| `writes.p95_wall_seconds` | 95th percentile write wall time, or `null` with no writes |
| `writes.cpu_seconds`, `writes.lock_wait_seconds` | Totals across those writes |
| `writes.hierarchy_sync_seconds`, `writes.outbox_write_seconds`, `writes.refresh_record_seconds`, `writes.event_append_seconds` | Substep totals across those writes |
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
| No `.request-metrics.jsonl` in the ledger folder | The server has not started on code from pull request 4331, or it cannot write the folder. Check the stderr line above |

Related: [Ledger load gate](ledger-load-gate.md) grades write latency on a
scratch server in CI; this page reads the live server.
