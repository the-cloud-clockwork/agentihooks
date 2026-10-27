---
title: Shared ledger database sessions
parent: Reference
nav_order: 48
---

# Shared ledger database sessions
{: .no_toc }

When `AGENTIHOOKS_SWARM_DATABASE_URL` is set, every ledger server and swarm process stores its ledgers in one shared
Postgres database through `PostgresLedgerRepository`. Writers take turns through one writer lock. Every connection
asks the server to end a session that stalls, so a client that dies or gives up while it holds or waits for that lock
cannot stop ledger writes for every swarm on the database. The database also stores the oldest writer version it
allows, and refuses ledger writes from older code that would make every replica rebuild a whole ledger.

1. TOC
{:toc}

---

## The writer lock

A ledger write opens its transaction with the SQLite statement `BEGIN IMMEDIATE`. On the shared database the
repository sends `SELECT pg_advisory_xact_lock(1279607879)` instead (`WRITER_LOCK`, `0x4C454447`). The lock is held
until the transaction commits or rolls back, so one writer at a time changes ledger rows, as with SQLite.

On 20 October one session took this lock and sat idle in transaction for over seven minutes while 40 to 65 writers
queued behind it. Every ledger write in three swarms hung, CLI commands timed out and the Doctor tick reached its
450 second deadline. After that backend was ended by hand, six more backends took the lock in turn and sat idle,
because each one belonged to a client that had already given up waiting. The server ran with
`idle_in_transaction_session_timeout` at 0, so nothing ended them.

## Settings every connection sends

`Pool.take` opens each connection with these libpq settings (`SESSION` in the Postgres repository module):

| Setting | Value | What the server does |
|---|---|---|
| `lock_timeout` | `10s` | Cancels any lock wait after 10 seconds, the writer lock wait included. |
| `idle_in_transaction_session_timeout` | `15s` | Ends a session that sits idle inside an open transaction for 15 seconds, which releases its writer lock. |
| `statement_timeout` | `60s` | Cancels any single statement after 60 seconds. |
| `agentihooks.ledger_writer_version` | `1` | The writer version the writer floor trigger compares with the oldest version the database allows. |
| `keepalives`, `keepalives_idle`, `keepalives_interval`, `keepalives_count` | `1`, `30`, `10`, `3` | Client TCP keepalives: a probe after 30 idle seconds, then every 10 seconds, giving up after 3 unanswered probes. |

The first four travel as `-c` startup options, so they apply to the session from its first statement and need no
change on the server. They override the server defaults for ledger sessions only; other clients of the same database
keep the server's settings.

The schema setup that runs once per database address per process lifts the statement limit for its own transaction
with `SET LOCAL statement_timeout = 0`, because the first start after an upgrade can build row indexes over every
stored ledger. It still takes the writer lock and still falls under the idle limit.

## What happens to a stalled writer

| Case | Outcome |
|---|---|
| A holder goes idle in its transaction (its process hangs, is killed behind a proxy that keeps the server side open, or its client gave up while queued and was then granted the lock) | The server ends the session after 15 idle seconds and the lock passes to the next writer. |
| A writer waits for the lock longer than 10 seconds | The server cancels the wait. The repository rolls back and asks again, three waits in all. |
| Three waits in a row are cancelled | The write raises `sqlite3.OperationalError("the ledger writer lock was not granted in 3 waits of 10 seconds")`. |
| A statement runs longer than 60 seconds | The server cancels it. The error reaches the caller as `sqlite3.DatabaseError` and the transaction rolls back. |
| The client host or network disappears | Keepalives let the client side notice a dead peer; on the server side the idle limit ends a transaction left open. |

The idle limit (15 seconds) is shorter than a writer's whole patience (three waits of 10 seconds), so a waiting writer
outlasts an idle holder and commits after it is ended.

The retry happens only where it cannot discard work: the writer lock is refused inside a transaction that is already
open (`cannot start a transaction within a transaction`), so every caller takes it as its transaction's first
statement and a rolled back wait never loses earlier statements.

A session the server ended fails its next statement. When the connection goes back to the pool its rollback fails,
so `Pool.give` closes it instead of keeping it, and the next write opens a fresh connection.

## Inspect the shared database

List every session holding or waiting for the writer lock, with how long each has been in its current state:

```bash
psql "$AGENTIHOOKS_SWARM_DATABASE_URL" -c "
SELECT a.pid, l.granted, a.state, now() - a.state_change AS in_state, a.client_addr
FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.locktype = 'advisory' AND l.classid = 0 AND l.objid = 1279607879 AND l.objsubid = 1
ORDER BY l.granted DESC, a.state_change"
```

- One row with `granted` true is the current holder. With these settings it is never `idle in transaction` for more
  than about 15 seconds.
- Rows with `granted` false are queued writers; each waits at most 10 seconds per attempt.

Confirm a ledger connection carries the settings, from a checkout with the ledger dependencies installed:

```bash
python -c '
import os
from scripts.swarm_ledger.repository import postgres
c = postgres.Connection(os.environ["AGENTIHOOKS_SWARM_DATABASE_URL"])
for name in ("lock_timeout", "idle_in_transaction_session_timeout", "statement_timeout"):
    print(name, c.execute(f"SHOW {name}").fetchone()[0])
c.close()'
```

Expected output:

```
lock_timeout 10s
idle_in_transaction_session_timeout 15s
statement_timeout 1min
```

A `psql` session of your own shows the server defaults, not these values, because the settings belong to each ledger
connection.

The server log records each limit it enforces:

| Log line | Meaning |
|---|---|
| `terminating connection due to idle-in-transaction timeout` | An idle holder or idle transaction was ended. |
| `canceling statement due to lock timeout` | A writer's lock wait was cancelled and the writer asked again or gave up. |
| `canceling statement due to statement timeout` | A statement ran past 60 seconds. |

Count these lines over a window rather than only refused writes: before this change no lock timeout existed, so a
frozen writer left no refusal at all.

## Measure stuck holders

The figure this change moves is minutes per day that any writer lock holder sits idle in transaction for over 30
seconds, read from repeated samples of the holder query above. On 20 October from 06:54 to 07:05 UTC it was 11
minutes. With the idle limit at 15 seconds every sample should show no holder idle for over 30 seconds. A sample
loop for a window:

```bash
while sleep 30; do
  psql -At "$AGENTIHOOKS_SWARM_DATABASE_URL" -c "
  SELECT now(), a.pid, now() - a.state_change
  FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
  WHERE l.locktype = 'advisory' AND l.objid = 1279607879 AND l.granted
    AND a.state = 'idle in transaction' AND now() - a.state_change > interval '30 seconds'"
done | tee -a writer-lock-idle.log
```

Each printed line is one sample with a stuck holder; at a 30 second interval, lines divided by two give the
minutes.

## The writer floor

Every write to a ledger on the shared database raises that ledger's `generation`. A current writer also records the
rows it changed in `row_changes` under the new generation, and every other ledger server reading the same database
(a replica) refreshes only those rows. A writer that bumps the generation without recording changed rows makes every
replica rebuild the whole ledger after each of its writes. Ledger task t690 found one workstation ledger server on old
code doing this until it was replaced on 22 October: every hosted replica rebuilt the 26 MB ledger after each write it
made. Nothing reported it; reads only slowed down.

The writer floor refuses such a write at the database. While the refused writer stays connected, the next allowed
write names it in a ledger alert for the master.

### How it works

| Part | What it does |
|---|---|
| `writer_floor` table | One row, `name = 'ledger'`, holding the oldest writer version the database allows. |
| Floor setup | The once per address schema setup runs `INSERT INTO writer_floor VALUES ('ledger', REQUIRED)` with `ON CONFLICT ... GREATEST(...)`, so a writer can raise the stored version but never lower it. |
| Writer version | Each connection sends `-c agentihooks.ledger_writer_version=VERSION` (see [Settings every connection sends](#settings-every-connection-sends)). Code from before the floor sends nothing and counts as version 0. |
| Check in the writer | `PostgresLedgerRepository._write` reads the stored version inside the write transaction, under the writer lock, before it changes anything. Below it, the write is refused and nothing is written. |
| `ledger_writer_floor` trigger | Runs `BEFORE UPDATE ON ledgers` for each row whose `generation` changes. It refuses the update when the session's writer version is below the stored version and no `row_changes` row exists for that ledger and the new generation. This catches code that has no check of its own. |

`VERSION` (what this code writes as) and `REQUIRED` (the floor this code stores) are constants in the Postgres
repository module. Both are `1`: version 1 is the first writer that records changed rows.

An old writer that still records its changed rows passes the trigger, because replicas can refresh from those rows.
Only an unrecorded generation bump is refused.

### The refusal

The writer sees:

```
ledger writer version 0 is below version 1, the oldest the shared database allows; update its code and restart it
```

The first number is the refused writer's version, the second the stored floor. The trigger raises it with SQLSTATE
`LW001`. In current code the error reaches the caller as `postgres.Refused`, a subclass of `sqlite3.DatabaseError`, so
existing handlers of database errors still catch it. Code older than the floor gets a database error whose text
contains the same message, after the driver's `ERROR:` prefix.

The refused transaction rolls back: the ledger's generation, rows and events stay as they were, and no replica
rebuilds anything because of it.

### The ledger alert

A refused transaction cannot write an alert, since it rolls back. Before raising the error, the refused session takes
a session advisory lock that outlives the rollback and lasts until that connection closes. The lock key carries the
refusal: class id `0x4C574652` (`1280788050`), object id `writer version * 65536 + floor`, sub id 2.

Each allowed write then lists those locks, with the client address of each refused session, and raises one open
`writer` alert per refused writer on the ledger it is writing, targeted at the master:

```
The ledger writer at 10.0.0.5 was refused: ledger writer version 0 is below version 1, the oldest the shared database allows; update its code and restart it
```

A session with no client address, such as one on a local socket, shows as `an unseen client`.

- The alert lands on the next ledger written by an allowed writer, which may be a different ledger from the one the
  refused writer tried to change. Each ledger written while the refused session stays connected gets its own alert.
- An open alert with the same text is not raised twice. A closed one is not raised again on that ledger while it
  stays in the ledger's alert list; a different address, version or floor makes a new alert. Once closed alerts beyond
  the kept 200 are dropped from the list, the same refusal can raise a new alert.
- Once the refused writer's connection closes, its advisory lock is released and no further alert is raised.

### Inspect the floor and refused writers

Read the stored floor:

```bash
psql "$AGENTIHOOKS_SWARM_DATABASE_URL" -At -c "SELECT version FROM writer_floor WHERE name = 'ledger'"
```

On current code this prints `1`. No row means no writer on floor code has set the database up yet.

List refused writers still connected, with the version each sent and the floor it hit:

```bash
psql "$AGENTIHOOKS_SWARM_DATABASE_URL" -c "
SELECT DISTINCT COALESCE(host(a.client_addr), 'an unseen client') AS writer, a.pid,
       l.objid::BIGINT / 65536 AS version, l.objid::BIGINT % 65536 AS floor
FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.locktype = 'advisory' AND l.classid = 1280788050 AND l.objsubid = 2
  AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())"
```

No rows means no refused writer is connected.

A generation bump is unrecorded when a ledger's `generation` advances and `row_changes` holds no row for that ledger
and generation. The floor refuses these from writers below it; a writer at or above the floor is trusted to record its
rows. Sample the newest generation of every ledger for unrecorded bumps over a window:

```bash
while sleep 5; do
  psql -At "$AGENTIHOOKS_SWARM_DATABASE_URL" -c "
  SELECT l.slug, l.generation FROM ledgers l
  WHERE NOT EXISTS (SELECT 1 FROM row_changes r WHERE r.slug = l.slug AND r.generation = l.generation)"
done | sort -u | tee unrecorded-bumps.log
```

Stop the loop at the end of the window; each line in the log is one unrecorded bump. A sample sees only each ledger's newest generation,
so bumps between samples go uncounted; a ledger nobody has written since it was created can show once and is not a
bump.

Ledger task t691 measured with its own generation poll probe on 23 October, which counts every bump as well as the
unrecorded ones that this query alone shows: 286 bumps in the hour before the floor reached the hosted ledger server
and 344 in the hour after, none unrecorded in either. The old workstation
writer had already been replaced on 22 October, so the floor guards against its return rather than moving a live
count.

### Raise the floor

When a change makes an older writer harmful to replicas:

1. In the Postgres repository module, raise `VERSION` and `REQUIRED` together to the new number and say what the
   version adds in the comment above them.
2. Merge it to `dev`. The first process on the new code to set up the schema raises the stored floor; from then on
   every writer below it is refused.
3. Restart or update every ledger server on the shared database, workstation servers included. Watch for `writer`
   alerts on the ledgers and run the refused writers query above.

Never raise `REQUIRED` above `VERSION`: that code would refuse its own writes.

The floor is never lowered by code. Rolling back to a release below the stored floor stops that release from writing.
Lowering it is a manual statement on the database, `UPDATE writer_floor SET version = <n> WHERE name = 'ledger'`, and
only makes sense after the newer writers are gone.

## Configuration

The values are constants in the Postgres repository module (`LOCK_WAIT`, `LOCK_TRIES`, `SESSION`, `VERSION`,
`REQUIRED`) and change only through a pull request. No environment variable overrides them. The only setting is the database address,
`AGENTIHOOKS_SWARM_DATABASE_URL`; without it the ledger uses SQLite in `LEDGER_DIR` and none of this applies. See the
[Docker Compose quick start](../getting-started/compose-quick-start.md) for where the address is set.

## Limitations

- The idle limit applies to every ledger transaction on the shared database, not only writers. Ledger writes,
  snapshots and the hierarchy backfill do only in-process work between statements. A migration restore on the
  receiving server writes and reads back imported media inside its transaction; a restore stalled past 15 seconds
  ends as a server error, its transaction rolls back and the sender can send it again.
- `lock_timeout` applies to every lock wait, not only the writer lock. Writers serialize on the writer lock first,
  so other waits are not expected; one would surface as `sqlite3.DatabaseError`.
- The settings travel as libpq startup options. A connection pooler between the ledger and Postgres that drops
  startup options would leave them unset. The shared database is reached directly today; check with the `SHOW`
  command above after putting anything in between.
- A process that already holds a connection opened before an upgrade keeps that connection's settings until it is
  replaced; restart ledger servers after deploying a change to these values.
- The writer floor trigger guards only generation changes on the `ledgers` table. Code older than the floor that writes
  other tables without bumping the generation is not refused by the database.
- A refused writer is named by its client address only while its connection stays open. A writer that exits after its
  refusal leaves no alert if no allowed write happens before it disconnects; its own error is then the only record.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Writes fail with `the ledger writer lock was not granted in 3 waits of 10 seconds` | Writers queued for over 30 seconds: a long write, a schema setup building indexes, or many writers at once | Run the holder query to see who holds the lock and for how long; retry the command once the queue drains |
| `cannot start a transaction within a transaction` | Code asked for the writer lock after its transaction had already run a statement | Take the writer lock as the transaction's first statement |
| A holder shows `idle in transaction` for minutes | The session did not receive the settings, for example through a pooler that drops startup options, or the process predates this change | Run the `SHOW` check; restart the ledger server on current code; end the backend with `SELECT pg_terminate_backend(<pid>)` only as a stopgap |
| `canceling statement due to statement timeout` in the server log | One statement ran past 60 seconds | Find the statement in `pg_stat_activity.query` while it runs and fix the query or its index in code |
| Errors containing `terminating connection due to idle-in-transaction timeout` on a client | That client held a transaction open for 15 idle seconds | Look for work between statements in its transaction (file I/O, network calls) and move it outside |
| Writes fail with `ledger writer version N is below version M, the oldest the shared database allows` | That ledger server runs code older than the stored writer floor | Pull current code on that host and restart its ledger server; confirm with the refused writers query |
| A `writer` alert names `The ledger writer at <address> was refused` | A ledger server at that address tried to write on old code and is still connected | Update and restart the ledger server at that address, then close the alert |
| Every write fails with the floor refusal right after a rollback | The release rolled back to is older than the floor a newer release stored | Roll forward to a release at or above the stored floor; lower it by hand only after every newer writer is gone |
| Hosted ledger reads slow down after writes from one host, with no refusal | A writer bumps generations with no changed rows while the floor is not set, for example before the schema setup ran | Read the stored floor; restart any ledger server on floor code so its setup stores it |

## Verification in CI

The `Writer lock` workflow (`writer-lock.yml`) runs `tests/swarm_ledger/test_postgres_writer_lock.py` against a
Postgres 16 service container. One connection takes the writer lock and stays idle while a second writer waits; the
test passes when the second writer commits within 30 seconds and the holder's backend is gone.
`tests/swarm_ledger/test_postgres_repository.py` runs the translated statements against the same service: every
hierarchy read must match the SQLite repository, and an imported ledger, a write with its events and the relay
journal must round trip. The Tests workflow calls it on every pull request push, on pushes to `dev` and on manual
dispatch, `Gate — Required` needs it on pull requests, and its step fails unless all fourteen writer lock, outbox,
writer floor and repository scenarios ran. Unit tests in `tests/swarm_ledger/test_postgres.py` pin the connection settings and the
three-wait retry.

`tests/swarm_ledger/test_postgres_writer_floor.py` runs the writer floor in the same workflow against real Postgres:

- A current writer stores floor version 1 and writes.
- With the stored floor raised above the writer's version, the write is refused with the message above, the
  generation does not move, and the next allowed write raises one open `writer` alert for the master.
- A plain connection with no writer version that bumps a generation without changed rows is refused with
  `ledger writer version 0 is below version 1`, and a replica reading the ledger afterwards serves it without a full
  rebuild.
- The same old connection passes when it records its changed rows first.

`tests/swarm_ledger/test_postgres_floor.py` covers the refusal text, the mapping of `LW001` to `Refused`, the
statements the check runs, and the alert rules without a database. Pull request
[#6106](https://github.com/the-cloud-clockwork/agentihooks/pull/6106) delivered the floor.

Delivery evidence for pull request
[#5165](https://github.com/the-cloud-clockwork/agentihooks/pull/5165):

- Red, unit tests on head `e37e07d35`:
  [run 39385740990](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39385740990), three failures:
  connect received no settings, and a lost lock wait raised a bare `DatabaseError` with no retry.
- Green, every check on head `93aa21695`:
  [run 39387418173](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39387418173).
- Real Postgres 16, base against head, in
  [run 39386797215](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39386797215) of a one-off probe
  workflow on a throwaway branch, kept nowhere on `dev`. Pull request
  [#5208](https://github.com/the-cloud-clockwork/agentihooks/pull/5208) committed the scenario as a test, and
  [#5395](https://github.com/the-cloud-clockwork/agentihooks/pull/5395) moved it into the `Writer lock` workflow. The
  base printed
  `FAIL second writer still waiting after 90s behind an idle holder`; the head printed
  `PASS second writer committed after 15.0s; idle holder ended by the server`.

Inspect a workflow run's scenario result:

```bash
gh run view <run id> --log | grep -E 'passed|failed|FAIL|PASS'
```

## Related pages

- [Deployment modes](deployment-modes.md): which processes share the ledger database
- [Docker Compose quick start](../getting-started/compose-quick-start.md): setting `AGENTIHOOKS_SWARM_DATABASE_URL`
- [Presentation layer architecture](presentation-layer.md): the `LedgerRepository` interface behind both stores
