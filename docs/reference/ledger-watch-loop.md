---
title: Ledger server watch loop
parent: Reference
nav_order: 43
---

# Ledger server watch loop

The ledger server keeps its background maintenance loop running when the
scheduler cannot create its machine lock directory. It logs the failure once
and uses ordinary sleep between passes for the rest of that server process.
No operator action is needed to enable the fallback.

[PR #4704](https://github.com/the-cloud-clockwork/agentihooks/pull/4704)
delivered this filesystem failure handling. Before the fix, an unwritable
home cache could terminate `watch_ledgers` after its first pass while HTTP
writes continued to succeed. A successful write alone therefore does not
establish that background maintenance is working.

## Maintenance and waiting

`watch_ledgers` checks for code reloads when enabled, tidies the bin, periodically
bins closed ledgers and expires alerts, replays unrelayed writes, samples
streams and journals request metrics. The scheduler failure no longer prevents
later passes from reaching those operations. Each operation retains its own
cadence; fallback does not make every operation run on every pass.

The loop's default wait interval is **2.0 seconds**, after the pass finishes.
With a working scheduler, `scheduled_wait` uses the scheduler timer alone.
On an `OSError`, including `PermissionError` or `NotADirectoryError`, it writes
this warning to stderr with the exception detail:

```text
scheduler wait unavailable, sleeping instead: <exception detail>
```

That failed wait and every subsequent wait use `time.sleep(interval)`. The
process remembers that the scheduler is unavailable: it neither retries the
scheduler nor repeats the warning on later passes. Correcting permissions
while that process is alive does not switch it back. A fresh server process
tries the scheduler again.

If scheduler startup fails after a process timer has been registered, `Timer`
retires its entry, deletes its wake key and closes the Redis connection it owns
before re-raising. A caller-supplied connection remains caller-owned. Retirement
allows the scheduler to remove the registered entry on a later due-work pass.

Current code also falls back on Redis `NoPermissionError`. Other failures,
including Redis connection loss and `RuntimeError`, still propagate from
`scheduled_wait`; this is not general recovery for every watch-loop failure.

## Configuration

The fallback is automatic and has no dedicated flag, retry interval or
alternative lock-directory setting. The relevant existing inputs are:

| Input | Behavior |
|-------|----------|
| Server process home | Scheduler locks remain beneath `~/.cache/agentihooks/scheduler`. The fallback does not relocate them. |
| Redis endpoint and hive identity | `runtime_dir` selects a hashed subdirectory for a nondefault endpoint or hive namespace. The default local endpoint without a hive uses the base directory. |
| `SWARM_RELOAD=1` | Enables the watch loop's code-change reload check. Other values disable it; the Compose ledger service sets `0`. |
| `watch_ledgers(interval=2.0)` | Internal Python argument controlling the wait between passes; it is not a new CLI option or environment variable. |

For a deployment that should use scheduler timers, provide a writable home
cache for the ledger service's runtime user through its deployment definition.
Do not repair the running container in place. The fallback also permits the
maintenance loop to continue when that cache is intentionally unwritable.
See [Deployment modes](deployment-modes.md) for service placement and Redis
configuration.

For example, this Compose service environment disables code reload checking;
it does **not** disable maintenance or the automatic sleep fallback:

```yaml
services:
  swarm:
    environment:
      SWARM_RELOAD: "0"
```

## Inspect a container without changing it

Use the actual container name and a known UTC start time to keep log comparisons
within one server start. These commands only read logs:

```bash
watch_container=agentihooks-ledger-ledger-1
watch_start=2026-10-19T06:53:00Z

docker logs --since "$watch_start" "$watch_container" 2>&1 \
  | grep -F 'scheduler wait unavailable, sleeping instead'

docker logs --since "$watch_start" "$watch_container" 2>&1 \
  | grep -c 'in scheduled_wait'
```

The second command reproduces the source task's count of scheduler-wait
tracebacks per server start. `grep -c` prints `0` and exits with status `1` when
there are no matches. Inspect the surrounding traceback before attributing a
match to the lock directory. One fallback warning with no repeated scheduler
tracebacks is expected after an unwritable-cache failure; absence of a warning
can also mean the scheduler worked normally.

The delivered PR reported **one watch-loop death before the fix** and left the
post-deployment count unmeasured. Its CI evidence below proves controlled loop
continuation, not a measured zero for the live container. Preserve the start,
end and server identity when recording an operational after window.

If maintenance stops despite successful writes, inspect the server's exception
logs. A Redis connection failure or another exception outside the handled
cases needs its own diagnosis. Repeated fallback warnings can indicate multiple
server processes or restarts; compare process starts before treating them as
repeated warnings from one loop. A restart is needed to retry scheduler waiting
after repairing its deployment configuration.

## Verify in CI

Dispatch the focused proof against a pushed branch; run no tests on the shared
swarm host:

```bash
gh workflow run proofs.yml \
  --ref my-pushed-branch \
  -f proof=tests \
  -f tests='tests/swarm_ledger/test_watch_loop_scheduler.py tests/scheduler/test_process_timer.py'
```

The filesystem regression makes the isolated home cache unusable as a directory
and observes **three passes**, three sleeps of **2.0 seconds** and exactly one
fallback warning. A control checks that a working scheduler adds no plain sleep.
The timer regression checks retirement and removal of entries and wake keys
after scheduler startup fails. Current watch-loop tests also cover Redis
permission refusal and propagation of connection loss and other exceptions.

The original delivery retained these runs:

- [Red watch-loop proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39216336325):
  the unpatched loop fails on the unusable lock directory.
- [Green watch-loop proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39216507855):
  the fallback lets subsequent passes run.
- [Green timer cleanup proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39216886575):
  failed scheduler startup does not leave an active process timer behind.

For request journal inspection, see [Ledger request metrics](ledger-request-metrics.md).
For CLI event-stream replay, see [Ledger watcher replay verification](watch-ledger-replay.md);
that client watcher is separate from this server maintenance loop.
