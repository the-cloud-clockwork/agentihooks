---
title: Tick inbox, llm, metrics and host adapters
parent: Reference
nav_order: 64
---

# Tick inbox, llm, metrics and host adapters

The swarm tick reaches four outside systems through ports in `scripts.tick.ports`: the durable inbox store, the
decision classifier, the local metrics spool and the host readings and background jobs. Each port has one adapter in
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

The adapters exist and are tested; no tick pass builds them yet. Steps receive them once the tick engine passes a
`Tools` bundle to each step.

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
same function, so all four refuse with the same message.

## Inbox adapter

`StoreInbox(store, sender)` wraps an `InboxStore` and acts as one sender, normally the tick's own address.

```python
from scripts.inbox.store import InboxStore
from scripts.tick.adapters.inbox import StoreInbox
from scripts.tick.deadline import Deadline

port = StoreInbox(InboxStore(redis_client), "tick@rig-grade-swarm")
items = port.read("eng-1@rig-grade-swarm", deadline=Deadline.after(2.0))
item = port.send("eng-1@rig-grade-swarm", "wake", deadline=Deadline.after(2.0))
port.close(item.id, deadline=Deadline.after(2.0))
```

| Operation | Store call | Returns |
|---|---|---|
| `read(address)` | `store.inbox(address)` | The address's items |
| `send(address, body)` | `store.send(sender, address, body)` | The new item |
| `close(item_id)` | `store.close(item_id, sender, "done")` | The closed item, or `None` |

`send` passes no `ref`, `fyi`, `task` or `item_id`, and `close` always closes as `done` with the adapter's sender as
closer. A step that needs another outcome uses the store's own commands outside the tick.

## Llm adapter

`ApiLlm(model, url, backend=DecisionsApiBackend)` asks the decisions API one typed request. Each call builds a fresh
backend with `deadline.remaining` as its HTTP timeout, so the classifier request ends no later than the deadline.

```python
from scripts.tick.adapters.llm import ApiLlm
from scripts.tick.deadline import Deadline

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
  a bad request raises `ClassifierRequestError`.
- `backend` is any callable taking `(model, url, timeout_s)` and returning an object with `decide(request)`. Tests pass
  a recording function here.

The adapter calls one model with no failover. Failover across models and the decision log belong to
`hooks.classifier.decide`, which this port does not call.

## Metrics adapter

`SpoolMetrics(box, tables)` appends rows to the local metrics spool, the SQLite outbox in
`scripts.swarm.metrics_outbox`. `tables` maps the names a step uses to `Table` definitions.

```python
from scripts.swarm.metrics_outbox import Table
from scripts.tick.adapters.metrics import SpoolMetrics
from scripts.tick.deadline import Deadline

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
table's own columns. `outbox` is a `scripts.swarm.metrics_outbox.Outbox`.

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

from scripts.tick.adapters.host import local_host
from scripts.tick.deadline import Deadline

port = local_host(store, "rig-grade-swarm", os.environ)
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

## Declared effects

`Tools` in `scripts.tick.ports` holds one adapter per port. `Tools.for_effects(effects)` returns a copy whose ports
allow read operations always and write operations only on the ports named in `effects`.

```python
from scripts.tick.ports import Tools

tools = Tools(
    ledger=ledger,
    coordination=coordination,
    source_control=source_control,
    runtime=runtime,
    processes=processes,
    inbox=StoreInbox(inbox_store, "tick@rig-grade-swarm"),
    llm=ApiLlm("liquid", litellm_url),
    metrics=SpoolMetrics(outbox, tables),
    host=local_host(store, "rig-grade-swarm", os.environ),
)
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

A restricted port raises `TypeError("tools require a Deadline keyword argument")` when `deadline=` is missing or not a
`Deadline`, `TimeoutError("tool deadline expired")` when it has passed, and `AttributeError` for an operation outside
its port.

## Fakes for tests

`scripts.tick.adapters.fakes` has `FakeInbox`, `FakeLlm`, `FakeMetrics` and `FakeHost`. Each records a `Call` with the
operation, its arguments and the deadline, and answers from its `answers` dictionary keyed by operation. Each refuses
an expired deadline like the adapters. `fake_tools(effects)` builds a full `Tools` of fakes, restricted to `effects`
when given.

```python
from scripts.tick.adapters.fakes import FakeHost, fake_tools

tools = fake_tools(frozenset({"host"}))
host = FakeHost({"run": True})
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

Any relative import in a scanned module also fails, whatever it names, since the check cannot resolve it. The failure lists each
module path with the names it imported. Move the call behind a port operation in the adapter and call the port from
the step.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `TimeoutError: tool deadline expired` | The deadline passed before the call; no outside system was touched. | Give the step a larger budget or check `deadline.remaining` before a slow call. |
| `UndeclaredEffect: <port> is not a declared effect` | A step called a write operation on a port missing from its effects. | Declare the port in the step's effects. |
| `TypeError: tools require a Deadline keyword argument` | A restricted port was called without `deadline=`. | Pass the step's `Deadline` by keyword. |
| `KeyError: 'unknown metrics table: <name>'` | `emit` named a table the adapter was not built with. | Add the `Table` to the `tables` mapping. |
| `BackendFailure: <model>: timeout` from `classify` | The decisions API did not answer within `deadline.remaining`. | Raise the deadline, or read [Decision classifier](classifier.md#failover) for a model that answers faster. |
| `Unsupported(name)` from `host.read` or `host.run` | The host has no reading or job by that name. | Add it to the `JobHost` mappings or handle the missing capability in the step. |
| `test_no_tick_step_module_imports_an_adapted_implementation` fails | A tick module imports one of the adapted implementations. | Route the call through the matching adapter. |
