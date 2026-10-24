# Doctor verdict history

Each Doctor judgment appends a verdict event containing `finding`, `value`,
`note`, `by`, `at` and `measure`. Rejudging the same finding preserves both
events, including identical judgments written in the same millisecond.
The current finding record still holds the latest verdict and controls its
cooldown. Events belong to the Doctor that recorded them.

`VerdictStore.events(since, until, now_ms)` reads events in the half open
interval `[since, until)`. The log retains seven days, removes older events
on each judgment, and expires seven days after its last write. A window
starting before retention, ending in the future, or with an empty or reversed
interval is refused.

To compare a UTC day's events with the latest verdict per finding:

```bash
agentihooks doctor rig-grade-swarm verdict-log \
  --since 2026-10-23T00:00Z --until 2026-10-24T00:00Z
```

Choose a completed day within the last seven days. The command accepts the
watched ledger or its Doctor ledger and prints JSON: `event_count`,
`latest_count`, counts by verdict value, and the individual events. Latest
counts include only current verdicts whose judgment time falls in the window;
they can fall when a finding is judged again after that window.

Collection begins when the repair reaches the Doctor writer. Earlier verdict
values and notes were overwritten and cannot be backfilled. A window spanning
that transition has incomplete event counts. An empty log does not prove that
no judgments occurred before collection began.
