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
cannot stop ledger writes for every swarm on the database.

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

## Configuration

The values are constants in the Postgres repository module (`LOCK_WAIT`, `LOCK_TRIES`, `SESSION`) and change only
through a pull request. No environment variable overrides them. The only setting is the database address,
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

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Writes fail with `the ledger writer lock was not granted in 3 waits of 10 seconds` | Writers queued for over 30 seconds: a long write, a schema setup building indexes, or many writers at once | Run the holder query to see who holds the lock and for how long; retry the command once the queue drains |
| `cannot start a transaction within a transaction` | Code asked for the writer lock after its transaction had already run a statement | Take the writer lock as the transaction's first statement |
| A holder shows `idle in transaction` for minutes | The session did not receive the settings, for example through a pooler that drops startup options, or the process predates this change | Run the `SHOW` check; restart the ledger server on current code; end the backend with `SELECT pg_terminate_backend(<pid>)` only as a stopgap |
| `canceling statement due to statement timeout` in the server log | One statement ran past 60 seconds | Find the statement in `pg_stat_activity.query` while it runs and fix the query or its index in code |
| Errors containing `terminating connection due to idle-in-transaction timeout` on a client | That client held a transaction open for 15 idle seconds | Look for work between statements in its transaction (file I/O, network calls) and move it outside |

## Verification in CI

The `Writer lock` workflow (`writer-lock.yml`) runs `tests/swarm_ledger/test_postgres_writer_lock.py` against a
Postgres 16 service container. One connection takes the writer lock and stays idle while a second writer waits; the
test passes when the second writer commits within 30 seconds and the holder's backend is gone. The Tests workflow
calls it on pushes to `dev` and on manual dispatch, and its step fails unless all ten writer lock, outbox and writer
floor scenarios ran. Unit tests in `tests/swarm_ledger/test_postgres.py` pin the connection settings and the
three-wait retry.

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
