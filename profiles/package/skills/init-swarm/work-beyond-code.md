# Work Beyond Code

Read when a plan item changes a running system, moves a measured number, chases
a failure or answers a question, instead of shipping a change by pull request.

## Pick the kind

| The plan item | `--kind` | The agent closes it with |
|---|---|---|
| brings a running system to a named state: drain, migrate, restart, scale | `ops` | the command that shows the state, and its output |
| moves a measured number: latency, cost, hit rate, a limit | `tune` | the same command before and after, with both values |
| chases a failure whose cause is unknown | `troubleshoot` | the root cause, its evidence, and a fix pull request or a filed task |
| answers a question or makes a recommendation | `research` | a link to the written finding |
| ships a change to code, config or docs | none: `code` | a merged pull request |

These tasks run in the `eng` lane, sized like a code task: one agent life, one
worktree. A change the work needs still reaches the system through code; the
agent's prompt sends it through a pull request. A change big enough to review
on its own is a separate code task.

## Write the contract

Every task of these kinds carries all three flags; the ledger accepts fewer,
the agent then works without the missing part.

- `--must`: what is true when the task is done, measurable: "p95 latency
  under 300 ms at 200 requests a second", never "faster".
- `--check`: the one command, query or reading that shows it. Read only
  against the live system for `ops` and `tune`; the reproduction for
  `troubleshoot`; the named sources for `research`.
- `--judge`: who decides the check passed. `master` when the check's output
  decides it; `operator` when it needs a decision only the operator makes,
  such as accepting a recommendation or approving a cost.

`--description` still names the seam and the done condition; the seam here is
the system, the number, the symptom or the question.

## Worked example

Plan: the gateway drops requests under load. Find why and fix it, clear the
stuck jobs, size the worker pool, and decide whether to replace the queue
library. Phases: p1 find the cause, p2 fix and tune, p3 decide.

```bash
agentihooks ledger --slug <slug> task add t1 "Find why the gateway returns 502 under load" --lane eng --phase p1 \
  --description "Symptom: 502s from the gateway above 200 requests a second. Done when the cause is shown by evidence and fixed or filed." \
  --kind troubleshoot --must "The cause of the 502s is shown by evidence and fixed by a merged pull request or filed as a task" \
  --check "The load test that reproduced the 502s runs clean, or the filed task names the cause" --judge master
agentihooks ledger --slug <slug> task add t2 "Retry idempotent upstream calls in the gateway client" --lane eng --phase p2 \
  --description "Seam: the gateway client's upstream call. Done when a failed idempotent call is retried once, merged into dev." \
  --depends-on t1 --territory gateway/client
agentihooks ledger --slug <slug> task add t3 "Drain the stuck jobs from the gateway queue" --lane eng --phase p2 \
  --description "System: the gateway job queue. Done when no job older than an hour is left." \
  --kind ops --must "The gateway queue holds no job older than one hour" \
  --check "The queue depth by age query on the gateway dashboard" --judge master
agentihooks ledger --slug <slug> task add t4 "Size the gateway worker pool for p95 under 300 ms" --lane eng --phase p2 \
  --description "Number: gateway p95 latency at 200 requests a second. Done when it is under 300 ms." \
  --kind tune --depends-on t2 --must "Gateway p95 latency is under 300 ms at 200 requests a second" \
  --check "The p95 latency query on the gateway dashboard over 15 minutes of the load test, before and after" --judge master
agentihooks ledger --slug <slug> task add t5 "Compare the queue library with two replacements" --lane eng --phase p3 \
  --description "Question: keep or replace the gateway queue library. Done when a recommendation is written with its sources." \
  --kind research --must "A written recommendation weighs the current library against two others on throughput, operations and cost" \
  --check "The finding linked on the task names the sources behind each claim" --judge operator
```

`t2` is a code task: no `--kind`, no contract, written exactly as in a plan
that is all code.
