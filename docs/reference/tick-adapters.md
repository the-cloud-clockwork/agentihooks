---
title: Tick inbox, llm, metrics, host, runtime and processes adapters
parent: Reference
nav_order: 64
---

# Tick inbox, llm, metrics, host, runtime and processes adapters

The swarm tick reaches six outside systems through ports in `scripts.tick.ports`: the durable inbox store, the
decision classifier, the local metrics spool, the host readings and background jobs, the agent runtime with its herdr
panes, and the process table. Each port has one adapter in
`scripts.tick.adapters` and one fake in `scripts.tick.adapters.fakes`. Every operation takes a `Deadline` keyword
argument and is refused with `TimeoutError("tool deadline expired")` once that deadline has passed, before the
adapter touches Redis, the network, SQLite or a job.

Tick step modules never import the adapted implementations. A unit test in CI walks every module under
`scripts/tick` outside `scripts/tick/adapters` and fails on any direct import of them.

| Port | Adapter | Delegates to | Operations (read / write) |
|---|---|---|---|
| `Inbox` | `inbox.StoreInbox` | `scripts.inbox.store.InboxStore` | `read` / `send`, `close` |
| `Llm` | `llm.ApiLlm` | `hooks.classifier.api.DecisionsApiBackend` | none / `classify` |
| `Metrics` | `metrics.SpoolMetrics` | `scripts.swarm.metrics_outbox` spool and tables | none / `emit` |
| `Host` | `host.JobHost`, built by `host.local_host` | `scripts.swarm.host_budget`, `refresh_job`, `snapshot_job` | `read` / `run` |
| `Runtime` | `runtime.RouterRuntime` | `scripts.swarm_v2.runtime.base.RuntimeRouter` and herdr `pane list` | `inspect`, `panes` / `launch`, `stop` |
| `Processes` | `processes.PassProcesses` | `hooks.proc.processes` | `scan` / none |

The adapters exist and are tested; no tick pass builds a `Tools` bundle from them yet. Steps receive them once the
tick engine passes a `Tools` bundle to each step. The one exception is `PassProcesses`: every swarm tick pass already
uses it for the reaper's process scan, described under [One process scan per tick pass](#one-process-scan-per-tick-pass).

## Deadlines

`scripts.tick.deadline.Deadline` holds an absolute expiry on a clock, `time.monotonic` by default.

```python
from scripts.tick.deadline import Deadline

deadline = Deadline.after(5.0)
deadline.remaining
deadline.expired
step_deadline = deadline.child(2.0)
```

| Member | Meaning |
|---|---|
| `Deadline.after(seconds)` | Expires `seconds` from now. |
| `remaining` | Seconds left, never below `0.0`. |
| `expired` | True once the clock reaches the expiry. |
| `child(seconds)` | A deadline `seconds` from now, capped at the parent's expiry. |

Each adapter calls `scripts.tick.adapters.inbox.check(deadline)` first. The host, llm and metrics adapters reuse the
same function, and the runtime and processes adapters make the same check themselves, so all six refuse with the same
message.

## Inbox adapter

`StoreInbox(store, sender)` wraps an `InboxStore` and acts as one sender, normally the tick's own address.

```python
from scripts.inbox.store import connect
from scripts.tick.adapters.inbox import StoreInbox
from scripts.tick.deadline import Deadline

port = StoreInbox(connect(), "tick@rig-grade-swarm")
items = port.read("eng-1@rig-grade-swarm", deadline=Deadline.after(2.0))
item = port.send("eng-1@rig-grade-swarm", "wake", deadline=Deadline.after(2.0))
```

| Operation | Store call | Returns |
|---|---|---|
| `read(address)` | `store.inbox(address)` | The address's items |
| `send(address, body)` | `store.send(sender, address, body)` | The new item |
| `close(item_id)` | `store.close(item_id, sender, "done")` | The closed item |

`send` passes no `ref`, `fyi`, `task` or `item_id`. `close` closes as `done` with the adapter's sender as closer and
no outcome detail. The store accepts that only for an item sent with `fyi`; any other item raises
`InboxError("done needs an outcome naming where the work went")`, and closing an already closed item raises
`InboxError("message <id> is closed: <reason>")`. The sender must also act for the item's address.

## Llm adapter

`ApiLlm(model, url, backend=DecisionsApiBackend)` asks the decisions API one typed request. Each call builds a fresh
backend with `deadline.remaining` as its HTTP timeout, so the classifier request ends no later than the deadline.

```python
from hooks.classifier import YesNo
from scripts.tick.adapters.llm import ApiLlm
from scripts.tick.deadline import Deadline

questions = {
    "stuck": YesNo(
        "Is the agent waiting on something outside its task?",
        true="It waits on an outside event.",
        false="It is working on its task.",
    )
}
port = ApiLlm("liquid", "https://litellm.example")
result = port.classify(
    {"state": {"task": "t1", "phase": "build"}, "questions": questions},
    deadline=Deadline.after(8.0),
)
```

- `request["state"]` is passed as the decision state and `request["questions"]` as the typed questions dictionary
  described in [Decision classifier](classifier.md#calling-it). A request missing either key raises `KeyError`.
- The default backend posts to `<url>/v1/decisions` with the key in `AGENTIHOOKS_CLASSIFIER_LITELLM_KEY` and returns a
  `DecisionResult` with `answers`, `source` and `cost`.
- Backend errors are not caught. A timeout, refused key or unparsable answer raises `BackendFailure`; an HTTP 400 for
  a bad request raises `ClassifierRequestError`, except a 400 whose message names the context or tokens, which raises
  `BackendFailure` with `input too long`.
- `backend` is any callable taking `(model, url, timeout_s)` and returning an object with `decide(request)`. Tests pass
  a recording function here.

The adapter calls one model with no failover. Failover across models and the decision log belong to
`hooks.classifier.decide`, which this port does not call.

## Metrics adapter

`SpoolMetrics(outbox, tables)` appends rows to the local metrics spool, the SQLite outbox in
`scripts.swarm.metrics_outbox`. `tables` maps the names a step uses to `Table` definitions.

```python
import os

from scripts.swarm import metrics_outbox
from scripts.swarm.metrics_outbox import Outbox, Table
from scripts.tick.adapters.metrics import SpoolMetrics
from scripts.tick.deadline import Deadline

outbox = Outbox(metrics_outbox.spool_path(), metrics_outbox.settings(os.environ))
PASSES = Table("tick_passes", (("seconds", "Float64"),))
row = {
    "event_id": "pass-1",
    "ledger": "rig-grade-swarm",
    "ts_ms": 1793500000000,
    "plan": "",
    "phase": "",
    "slice": "",
    "task": "",
    "seconds": 41.0,
}
port = SpoolMetrics(outbox, {"passes": PASSES})
written = port.emit({"passes": [row]}, deadline=Deadline.after(1.0))
```

A row carries exactly the base columns `event_id`, `ledger`, `ts_ms`, `plan`, `phase`, `slice` and `task` plus the
table's own columns. `metrics_outbox.settings` reads the ClickHouse sink the outbox later flushes to.

- Every table in the call is written in one SQLite transaction through `append_many`, so either every row is spooled
  or none is. The adapter passes a checkpoint that writes nothing extra.
- A name missing from `tables` raises `KeyError("unknown metrics table: <name>")` before the spool is touched.
- Each table's `check` validates every row before the transaction opens; a row with missing or extra columns, a wrong
  type or no event id, ledger or positive time raises `ValueError` and nothing is written.
- Rows may be any iterable of dictionaries. `emit` returns the number of rows written.

Spooled rows reach ClickHouse on the outbox's own flush, not during `emit`.

## Host adapter

`JobHost(readers, jobs)` maps reading names to zero argument callables and job names to callables that take the
request mapping. An unknown reading or job returns `Unsupported(name)` instead of raising, so a step can tell a host
that lacks a capability from a failure.

`local_host(store, slug, environ)` wires one swarm on the controller host:

| Name | Kind | Calls | Returns |
|---|---|---|---|
| `sample` | reading | `host_budget.controller_host(environ)` | A `HostSample`, a `Room` when deployed, or `None` |
| `refresh` | job | `refresh_job.start(store, slug)` | `True` when it spawned the refresh job |
| `snapshot` | job | `snapshot_job.start(store, slug, request["now_ms"], environ)` | `True` when it spawned the snapshot job |

```python
import os
import time

from scripts.swarm.store import connect
from scripts.tick.adapters.host import local_host
from scripts.tick.deadline import Deadline

now_ms = int(time.time() * 1000)
port = local_host(connect(), "rig-grade-swarm", os.environ)
sample = port.read("sample", deadline=Deadline.after(1.0))
port.run({"job": "refresh"}, deadline=Deadline.after(1.0))
port.run({"job": "snapshot", "now_ms": now_ms}, deadline=Deadline.after(1.0))
port.read("disk", deadline=Deadline.after(1.0))
```

The last call returns `Unsupported("disk")`. The job name is read from `request["job"]` and converted to text; a
request without it returns `Unsupported("")`. The `snapshot` job raises `KeyError` without `now_ms`.

Both jobs start a background process and return at once. The deadline bounds only the start: `refresh` returns
`False` while its lock is held or within five minutes of the last start, and `snapshot` returns `False` unless the
swarm is running, its snapshot interval has passed and no snapshot job holds the lock.

## Runtime adapter

`RouterRuntime(router, agents, herdr=..., homes=...)` addresses agents by name. Agent operations go through a
`RuntimeRouter`, which picks the backend that owns the agent (local herdr, Kubernetes or a hive); the pane list goes
straight to herdr.

| Argument | Meaning |
|---|---|
| `router` | The `RuntimeRouter` that observes, spawns and terminates agents. |
| `agents` | A callable from an agent name to its `AgentRecord`, or `None` when the swarm records no such agent. |
| `herdr` | A callable taking herdr arguments and returning its parsed answer. Defaults to `scripts.swarm.runtime.herdr_call`. |
| `homes` | A callable from an `AgentRecord` to the scratch home folders passed to `terminate`. Defaults to no folders. |

```python
import os

from scripts.swarm.store import connect
from scripts.swarm_v2.runtime.routed import routed
from scripts.tick.adapters.runtime import RouterRuntime
from scripts.tick.deadline import Deadline

store = connect()
slug = "rig-grade-swarm"


def agent(name):
    return next((record for record in store.agents(slug) if record.name == name), None)


port = RouterRuntime(routed(os.environ).router, agent)
deadline = Deadline.after(5.0)
port.inspect("eng-1", deadline=deadline)
request = {"config": store.config(slug), "lane": "eng", "name": "eng-2", "task": {"id": "t1"}}
port.launch(request, deadline=deadline)
port.stop("eng-1", deadline=deadline)
port.panes(deadline=deadline)
```

| Operation | Router or herdr call | Returns |
|---|---|---|
| `inspect(handle)` | `router.observe(agent)` | The router's `Outcome` |
| `launch(request)` | `router.spawn(SpawnRequest(**request))` | The router's `Outcome` |
| `stop(handle)` | `router.terminate(agent, homes(agent))` | The router's `Outcome` |
| `panes()` | `herdr(["pane", "list"])` | The `panes` list from the answer |

- `inspect` and `stop` look the handle up through `agents` first. An unknown name returns
  `Outcome(operation, Status.UNAVAILABLE, "", detail="no agent named <handle>")` without calling the router.
- `launch` builds a `SpawnRequest` from the mapping, so the request needs exactly the keys `config`, `lane`, `name`
  and `task`; a missing or extra key raises `TypeError`.
- `panes` returns `[]` when herdr's answer carries no `panes` list.
- Router outcomes come back unchanged. A backend that is disabled, unknown or lacks the capability answers with its
  own `Status` such as `UNAVAILABLE`, `UNSUPPORTED` or `REFUSED`; the adapter does not raise for them.

The deadline is checked once before the call. The router and herdr receive no timeout, so a slow backend can run past
the deadline; each herdr call keeps its own thirty second subprocess limit. Forwarding the deadline
into the router and herdr is a follow up.

## Processes adapter

`PassProcesses(scan=hooks.proc.processes)` holds one process table for a tick pass. The table maps each process id
to a `hooks.proc.Process` with `pid`, `ppid`, `pgid`, `sid`, `start_time`, `state`, `comm` and `argv`.

```python
from scripts.tick.adapters.processes import PassProcesses
from scripts.tick.deadline import Deadline

shared = PassProcesses()
table = shared.scan(deadline=Deadline.after(10.0))
again = shared.scan(deadline=Deadline.after(10.0))
fresh = shared.rescan(deadline=Deadline.after(10.0))
```

| Operation | Behavior |
|---|---|
| `scan()` | Reads the process table on the first call and returns that same dictionary on every later call. |
| `rescan()` | Drops the held table and reads a fresh one, which later `scan` calls then return. |

`table is again` holds. Build a new `PassProcesses` for each pass; one instance never refreshes by itself. Both
operations refuse an expired deadline before reading `/proc`.

## One process scan per tick pass

`scripts.swarm.tick.tick` is wrapped in `scripts.swarm.reaper.scanning_pass`. The wrapper puts a new
`PassProcesses` in the `reaper.PASS` context variable for the length of the pass and clears it afterward, even when
the pass raises. Every reaper target selection in that pass, `retire` for a finished agent and `reap` for a process
holding a name the swarm no longer records, reads that shared snapshot instead of scanning the whole process table
again.

| Reaper read | Inside a pass | Outside a pass |
|---|---|---|
| Target selection in `targets` and `reap` | The pass snapshot, with a ten second deadline per read | A fresh `processes()` scan per call |
| Group membership at the start of `end` | A fresh full scan, once per termination | The same |
| Liveness while waiting after `SIGTERM` and `SIGKILL` | One `/proc/<pid>` read per signalled process | The same |

- When a selection wants a process id the snapshot lacks, the reaper reads that id from `/proc`. A live process,
  one started after the snapshot, replaces the pass snapshot with `rescan`; a process that has exited leaves the
  snapshot as it is.
- Membership stays a fresh scan so a process that joined the agent's process group after selection is still
  signalled and waited on.
- Each poll reads only the signalled processes. A process counts as gone when its `/proc` entry is missing, its start
  time differs from the one recorded at selection, or it is a zombie.
- Code that calls `retire` or `reap` outside a wrapped function, such as the swarm command line, scans the whole
  process table on every call.

Wrap any other function that makes several reaper selections in one go the same way:

```python
from scripts.swarm import reaper


@reaper.scanning_pass
def retire_all(names_and_pids):
    return [reaper.retire(name, pid, []) for name, pid in names_and_pids]
```

## Declared effects

`Tools` in `scripts.tick.ports` holds one adapter per port. `Tools.for_effects(effects)` returns a copy whose ports
allow read operations always and write operations only on the ports named in `effects`. In this example `ledger`,
`coordination`, `source_control`, `runtime` and `processes` stand for the adapters of the other five ports, and the
remaining names are built as in the sections above.

```python
from scripts.inbox.store import connect as inbox_connect
from scripts.swarm.store import connect as swarm_connect
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Tools

tools = Tools(
    ledger=ledger,
    coordination=coordination,
    source_control=source_control,
    runtime=runtime,
    processes=processes,
    inbox=StoreInbox(inbox_connect(), "tick@rig-grade-swarm"),
    llm=ApiLlm("liquid", "https://litellm.example"),
    metrics=SpoolMetrics(outbox, {"passes": PASSES}),
    host=local_host(swarm_connect(), "rig-grade-swarm", os.environ),
)
deadline = Deadline.after(5.0)
step_tools = tools.for_effects(frozenset({"inbox", "metrics"}))
step_tools.inbox.send("eng-1@rig-grade-swarm", "wake", deadline=deadline)
step_tools.host.read("sample", deadline=deadline)
step_tools.host.run({"job": "refresh"}, deadline=deadline)
```

The last line raises `UndeclaredEffect("host is not a declared effect")`.

| Port | Read operations | Write operations |
|---|---|---|
| `inbox` | `read` | `send`, `close` |
| `llm` | none | `classify` |
| `metrics` | none | `emit` |
| `host` | `read` | `run` |
| `runtime` | `inspect`, `panes` | `launch`, `stop` |
| `processes` | `scan` | none |

A restricted port raises `TypeError("tools require a Deadline keyword argument")` when `deadline=` is missing or not a
`Deadline`, `TimeoutError("tool deadline expired")` when it has passed, and `AttributeError` for an operation outside
its port.

## Fakes for tests

`scripts.tick.adapters.fakes` has `FakeInbox`, `FakeLlm`, `FakeMetrics`, `FakeHost`, `FakeRuntime` and
`FakeProcesses`. Each records a `Call` with the
operation, its arguments and the deadline, and answers from its `answers` dictionary keyed by operation. Each refuses
an expired deadline like the adapters. `fake_tools(effects)` builds a full `Tools` of fakes, restricted to `effects`
when given. In `fake_tools`, the processes fake answers `scan` with an empty tuple.

```python
from scripts.tick.adapters.fakes import FakeHost, FakeRuntime, fake_tools

tools = fake_tools(frozenset({"host"}))
host = FakeHost({"run": True})
runtime = FakeRuntime({"panes": [{"pane_id": "w1:p1"}]})
```

`PassProcesses` takes its scan function as an argument, so a test can count scans without a fake:

```python
from scripts.tick.adapters.processes import PassProcesses

scans = []
shared = PassProcesses(lambda: scans.append(1) or {})
```

## The import check

`tests/tick/test_adapters.py::test_no_tick_step_module_imports_an_adapted_implementation` parses every `.py` file
under `scripts/tick` except those in `scripts/tick/adapters` and fails on any import, at any depth and including
`TYPE_CHECKING` blocks, of these modules or their children:

- `scripts.inbox`
- `hooks.classifier`
- `scripts.swarm.metrics_outbox`
- `scripts.swarm.host_budget`
- `scripts.swarm.refresh_job`
- `scripts.swarm.snapshot_job`

Any relative import in a scanned module also fails, whatever it names, since the check cannot resolve it.
The failure lists each module path with the names it imported. Move the call behind a port operation in the adapter
and call the port from the step.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `TimeoutError: tool deadline expired` | The deadline passed before the call; no outside system was touched. | Give the step a larger budget or check `deadline.remaining` before a slow call. |
| `UndeclaredEffect: <port> is not a declared effect` | A step called a write operation on a port missing from its effects. | Declare the port in the step's effects. |
| `TypeError: tools require a Deadline keyword argument` | A restricted port was called without `deadline=`. | Pass the step's `Deadline` by keyword. |
| `KeyError: 'unknown metrics table: <name>'` | `emit` named a table the adapter was not built with. | Add the `Table` to the `tables` mapping. |
| `BackendFailure: <model>: timeout` from `classify` | The decisions API did not answer within `deadline.remaining`. | Raise the deadline, or build `ApiLlm` with a model that answers faster. |
| `InboxError: done needs an outcome naming where the work went` from `inbox.close` | The item was not sent with `fyi`, and the adapter closes without an outcome. | Close the item with `agentihooks msg close <id> done <where the work went>` or answer it with `agentihooks msg reply <id> <text>`. |
| `Outcome(..., Status.UNAVAILABLE, "", detail="no agent named <handle>")` from `runtime.inspect` or `runtime.stop` | The `agents` lookup found no record for that name. | Check the name against the swarm's agent records; the router was not called. |
| `TypeError` from `runtime.launch` | The request mapping lacks one of `config`, `lane`, `name`, `task`, or carries another key. | Pass exactly those four keys. |
| `runtime.panes` returns `[]` | herdr answered without a `panes` list. | Run `herdr pane list` on the host and check the herdr server is up. |
| A reaped or retired process was not found in a tick pass | The process exited before selection, so its `/proc` entry was gone and the snapshot was kept. | No action; the process is already gone. |
| `Unsupported(name)` from `host.read` or `host.run` | The host has no reading or job by that name. | Add it to the `JobHost` mappings or handle the missing capability in the step. |
| `test_no_tick_step_module_imports_an_adapted_implementation` fails | A tick module imports one of the adapted implementations. | Route the call through the matching adapter. |
