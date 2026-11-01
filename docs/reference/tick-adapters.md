---
title: Tick adapters
parent: Reference
nav_order: 64
---

# Tick adapters

The swarm tick reaches nine outside systems through ports in `scripts.tick.ports`: the ledger server, the swarm
store's coordination keys, the source control provider, the durable inbox store, the decision classifier, the local
metrics spool, the host readings and background jobs, the agent runtime with its herdr panes, and the process table.
Each port has one adapter in `scripts.tick.adapters` and one fake in `scripts.tick.adapters.fakes`. Every operation
takes a `Deadline` keyword argument and is refused with `TimeoutError("tool deadline expired")` once that deadline has
passed, before the adapter touches Redis, the network, SQLite, git or a job.

Tick step modules never import the adapted implementations. A unit test in CI walks every module under
`scripts/tick` outside `scripts/tick/adapters` and fails on a direct import of the modules listed under
[The import check](#the-import-check).

| Port | Adapter | Delegates to | Operations (read / write) |
|---|---|---|---|
| `Ledger` | `ledger.ClientLedger`, built with `ledger.resource_client` | `scripts.swarm_ledger.api.client.ResourceClient` on the ledger server's export and mutate routes | `export` / `mutate` |
| `Coordination` | `coordination.StoreCoordination` | `scripts.swarm.store.RedisStore`, `scripts.swarm.lease` and `scripts.routing.reservation.LaunchSlots` | `read`, `capacity` / `lease`, `lock`, `configure`, `mark` |
| `SourceControl` | `github.GitHubSourceControl` | `scripts.swarm.github_reads.rest` (`gh api`) and `git ls-remote` | `change`, `checks`, `runs`, `queue_health`, `tags`, `capabilities` / `enqueue`, `merge`, `comment` |
| `Inbox` | `inbox.StoreInbox` | `scripts.inbox.store.InboxStore` | `read` / `send`, `close` |
| `Llm` | `llm.ApiLlm` | `hooks.classifier.api.DecisionsApiBackend` | none / `classify` |
| `Metrics` | `metrics.SpoolMetrics` | `scripts.swarm.metrics_outbox` spool and tables | none / `emit` |
| `Host` | `host.JobHost`, built by `host.local_host` | `scripts.swarm.host_budget`, `refresh_job`, `snapshot_job` | `read` / `run` |
| `Runtime` | `runtime.RouterRuntime` | `scripts.swarm_v2.runtime.base.RuntimeRouter` and herdr `pane list` | `inspect`, `panes` / `launch`, `stop` |
| `Processes` | `processes.PassProcesses` | `hooks.proc.processes` | `scan` / none |

The adapters exist and are tested; no tick pass builds a `Tools` bundle from them yet. Steps receive them once the
tick engine passes a `Tools` bundle to each step. Two adapters are already in use outside a bundle: every swarm tick
pass uses `PassProcesses` for the reaper's process scan, described under
[One process scan per tick pass](#one-process-scan-per-tick-pass), and `scripts.swarm.stack.change` reads one pull
request through `GitHubSourceControl`, described under [Source control adapter](#source-control-adapter). The ledger
and coordination adapters have no caller outside their tests. Every pass holds one lease heartbeat, described under
[One lease heartbeat per tick pass](#one-lease-heartbeat-per-tick-pass).

## Empty registry integration

[Pull request 9201](https://github.com/the-cloud-clockwork/agentihooks/pull/9201) connected the tick pass to
`scripts.tick.engine.run_group` before any production steps were registered. This section describes that delivery
boundary. Later changes register existing work as steps; the empty registry claim applies to the original delivery.

The integration added call sites around the existing work without moving it. Each reached site calls
`scripts.swarm.tick.run_group(name)`, which supplies the context to the engine. A completed wrapper pass reaches
the groups in this order:

| Group | Call site in the original integration |
|---|---|
| `before` | In `run_tick`, after timing retention and before checking whether the swarm is stopped. |
| `start` | At entry to `tick`, before loading the swarm configuration and its stopped early return. |
| `observe` | Before clearing the spawn hold and reconciling pane and execution evidence. |
| `agents` | Before reaping agents and recovering outages. |
| `masters` | Before waking or retiring masters. |
| `tasks` | Before reopening tasks and refreshing promotion state. |
| `place` | Inside the placement lock, before quota refresh and placement work. |
| `close` | Before the final settlement calls in the tick return expression. |
| `after` | In `run_tick`, after writing the last tick and heartbeat, before its normal return. |

These are reached call sites. Calling `tick` directly on a stopped swarm reaches only `start`. The wrapper calls
`before` even on its stopped path, which returns without reaching `tick` or `after`. An exception or another early
return before the normal wrapper return also skips `after`; it is not a cleanup hook in `finally`. The `close` group
precedes settlement, so reaching it does not prove that the whole wrapper completed.

### Usage and configuration

No operator flag, environment variable or swarm setting enables these call sites. In the original delivery,
contributors could invoke an individual site through the same helper the tick uses:

```python
from scripts.swarm.tick import run_group

run_group("observe")
```

This is an internal API example for an isolated development process at the empty registry delivery. The helper
returns `None` and discards the engine's step records. It does not run the surrounding legacy observation work.
Once steps are registered, calling a group can execute their effects; it is not an operator diagnostic command.

Every helper invocation constructs its own `Context` with these values:

| Value | Original integration behavior |
|---|---|
| `view` | A fixed view with an empty mapping. Every domain and key lookup returns `None`. |
| `deadline` | `Deadline.after(GROUP_BUDGET_S)`, freshly created for that call. |
| `GROUP_BUDGET_S` | The source constant `600.0` seconds, with no runtime override introduced by this change. |

For example, a step reading `ctx.view.read(Domain("tasks"), "t1")` receives `None` at this boundary. It cannot use
that view to read a live task, agent or ledger record. The integration supplies neither populated state nor an
adapter bundle to those reads. Adding such inputs requires a code change, as does changing the group budget.

The six hundred seconds belong to each group context. They are not a shared pass deadline or a new timeout around
the legacy calls between sites. See [Deadlines](#deadlines) for deadline semantics and
[Tick load budget](tick-load-budget.md) for the separate simulated whole pass budget.

### Verification and troubleshooting

With no registered steps, the engine has no step work to execute at these sites. The delivery preserved the golden
replay output. Its final head passed
[Tick equivalence run 41287855934](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/41287855934),
comparing the delivered head with its base. A deliberately dropped reap action failed
[run 41284317455](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/41284317455), showing that the
comparison detects a changed action. These are delivery proofs, not evidence that later registered steps leave
every pass unchanged.

Inspect the recorded result without running a local test or a live tick:

```bash
gh pr view 9201 --repo the-cloud-clockwork/agentihooks --json body,mergeCommit,state
gh run view 41287855934 --repo the-cloud-clockwork/agentihooks --json conclusion,headSha,url
```

On a hive, prefix each `gh` command with `python -m scripts.github_app_cli exec` to use its renewed application
token. The equivalence workflow retains a `tick-equivalence` artifact for seven days; after expiry, use the retained
run conclusion and pull request evidence. The group site tests cover order, stopped behavior, the empty view,
the fresh budget and execution of a registered marker step. Run regression checks through CI.

If an expected group is absent, check which return or failure ended the pass before that site. If `after` is absent,
check whether the wrapper reached its normal completion path. If a step sees no task data, the empty view explains
that result at this delivery boundary. If a later pass differs, inspect its registered steps and delivery revision
before applying the original empty registry equivalence claim.

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
| `live()` | `remaining`, or `TimeoutError("tool deadline expired")` once `expired` is true. |

The inbox adapter calls `scripts.tick.adapters.inbox.check(deadline)` first. The host, llm and metrics adapters reuse
the same function, the ledger and coordination adapters call `deadline.live()`, and the source control, runtime and
processes adapters make the same check themselves, so all nine refuse with the same message.

## Ledger adapter

`ClientLedger(slug, clients)` reads and writes one ledger, named by `slug`, through the ledger server's resource API.
`clients` is a callable that takes the seconds left on the deadline and returns a
`scripts.swarm_ledger.api.client.ResourceClient`. `resource_client(slug, service=False)` builds the production
callable from the same base address and credentials the swarm's `LedgerClient` uses; `service=True` asks for the
service credentials.

```python
from scripts.tick.adapters.ledger import ClientLedger, resource_client
from scripts.tick.deadline import Deadline

slug = "rig-grade-swarm"
port = ClientLedger(slug, resource_client(slug, service=True))
state = port.export(deadline=Deadline.after(10.0))
comment = {
    "op": "add",
    "id": "add-1a2b3c4d5e",
    "by": "swarm",
    "thread": "tasks/t1/comments",
    "text": "Checks passed.",
}
reply = port.mutate([comment], deadline=Deadline.after(10.0))
```

| Operation | Client call | Returns |
|---|---|---|
| `export()` | `ResourceClient.export(slug)`, `POST /api/v1/ledgers/<slug>/export` | The ledger export as a dictionary |
| `mutate(operations)` | `ResourceClient.mutate(slug, stamped)`, `POST /api/v1/ledgers/<slug>/operations` after a revision read of each target | The server's answer as a dictionary |

- Each call refuses an expired deadline before it builds a client, so no request is sent.
- The client is built per call with a request timeout of the smaller of the ledger request timeout
  (`REQUEST_TIMEOUT` in `scripts.swarm_ledger.ledger`, 10 seconds) and the seconds left on the deadline.
- That timeout bounds each HTTP request, not the call. `export` can fall back to a second, full read, and `mutate`
  reads the revision of each target and retries a conflicting write with a backoff, so one call can outlast its
  deadline.
- `mutate` stamps every operation with `controller_epoch` from `scripts.swarm.lease.epoch_for(slug)` and leaves the
  caller's list unchanged. With no epoch the operations go out as given. The epoch is resolved in this order:

| Context of the call | Epoch on each operation |
|---|---|
| Inside `lease.fencing(epoch)` | That epoch, for any ledger |
| Inside a tick pass, for the pass's own swarm | The epoch of the lease the pass holds |
| Inside a tick pass, for another swarm's ledger | The epoch of that swarm's live lease owner, or none when it has no live lease |
| Outside both | None |

The ledger server checks a stamped epoch against the live controller lease of the ledger written to and refuses a
stale one with HTTP 409 `stale_controller` and the message `the controller lease is stale`.
`LedgerClient` writes, the ones every tick pass makes today, resolve their epoch the same way.

## Coordination adapter

`StoreCoordination(store, slug, owner)` coordinates one swarm through its `RedisStore` on behalf of the controller
named `owner`, normally the hive id from `scripts.swarm.commands.hive_id`.

```python
from dataclasses import replace

from scripts.swarm.commands import hive_id
from scripts.swarm.store import connect
from scripts.tick.adapters.coordination import StoreCoordination
from scripts.tick.deadline import Deadline

port = StoreCoordination(connect(), "rig-grade-swarm", hive_id())
deadline = Deadline.after(5.0)
config = port.read(deadline=deadline)
held = port.lease(deadline=deadline)
token = port.lock("placement", deadline=Deadline.after(30.0))
port.configure(replace(config, max_eng=5), deadline=deadline)
slots = port.capacity(deadline=deadline)
port.mark("last-tick", 1793500000000, deadline=deadline)
port.mark("last-tick", None, deadline=deadline)
```

| Operation | Store call | Returns |
|---|---|---|
| `read()` | `store.config(slug)` | The swarm's `SwarmConfig` |
| `lease()` | `lease.acquire(store, slug, owner)` | The held `Lease`, or `None` while another controller holds it |
| `lock(name)` | Redis `SET <prefix>:<slug>:lock:<name> <token> NX PX <ms>` | A new random token, or `None` while another holder keeps the lock |
| `configure(settings)` | `store.config(slug)`, then `store.update(slug, **changes)` | The stored `SwarmConfig` |
| `capacity()` | `LaunchSlots(store.redis).counts()` | Launch slots in use, by slot name |
| `mark(name, value)` | Redis `SET <prefix>:<slug>:<name> <value>`, or `DEL` when `value` is `None` | Whether a key was set or deleted |

- Every operation refuses an expired deadline before it touches Redis.
- `lease` takes or keeps the controller lease with the same rules as the tick: it is refused while another owner
  holds a live lease or the swarm's controller host names another owner, and it raises the epoch when it takes over an
  expired lease. On a ledger served from a remote address the lease call goes to that server.
- `lock` holds the lock for the time left on the deadline, rounded down to whole milliseconds and never under one, so
  the lock expires by itself when the deadline does. Give the call a deadline sized to the work the lock guards. The adapter has no release operation; the lock ends at its expiry.
- `configure` compares every field of `settings` with the stored config and writes only the fields that differ, in
  one `update`. When nothing differs it writes nothing and returns the current config. `update` applies its own
  checks, so an invalid `state` or `autonomy` raises `SwarmError`.
- `capacity` counts the launch slot records under the swarm root, across every swarm, and drops index entries whose
  slot record has expired.
- `mark` stores `str(value)`. Deleting a mark that does not exist returns `False`.

## Source control adapter

The `SourceControl` port names no provider. Steps pass and receive the provider neutral records in
`scripts.tick.ports.source_control`, all frozen dataclasses, and only an adapter knows how a provider spells them.
`GitHubSourceControl` in `scripts.tick.adapters.github` is the one adapter today; another provider is one more class
with the same nine operations, and no step changes.

| Record | Fields | Meaning |
|---|---|---|
| `ChangeRef` | `repository`, `number` | One change, such as a pull request: `"owner/name"` and its number. |
| `ChangeState` | `reference`, `state`, `head`, `base`, `merged`, `url` | The change's provider state (`open` or `closed` on GitHub), head commit, target branch, whether it merged, and its web address. |
| `CheckState` | `name`, `status`, `conclusion`, `url` | One check on the change's head. `conclusion` is `None` until it completes. |
| `Run` | `id`, `name`, `head`, `status`, `conclusion`, `url` | One workflow run on the change's head. |
| `QueueEntry` | `reference`, `position` | The change's position in the merge queue after `enqueue`. |
| `QueueHealth` | `depth`, `states` | The number of queue entries and the state of each entry read. |
| `Merged` | `reference`, `sha` | The commit a merge produced. |
| `Tag` | `name`, `sha` | One remote tag and the object `git ls-remote` lists for it. |
| `Comment` | `reference`, `url` | The web address of a posted comment. |
| `Capabilities` | `merge_queue` | Whether the provider has a merge queue on the base branch. |

`GitHubSourceControl(repository, root, base, git=subprocess.run)` is a frozen dataclass:

| Argument | Meaning |
|---|---|
| `repository` | `"owner/name"`, the repository whose merge queue `queue_health` and `capabilities` read. |
| `root` | The working folder for every `gh` and `git` call; `tags` reads the `origin` remote of this checkout. |
| `base` | The branch whose merge queue `enqueue`, `queue_health` and `capabilities` use, normally `dev`. |
| `git` | The callable that runs `git`, with the `subprocess.run` signature. Tests pass a recorder here. |

```python
from pathlib import Path

from scripts.tick.adapters.github import GitHubSourceControl
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Unsupported
from scripts.tick.ports.source_control import ChangeRef

port = GitHubSourceControl("the-cloud-clockwork/agentihooks", Path.cwd(), "dev")
pull = ChangeRef("the-cloud-clockwork/agentihooks", 9166)
deadline = Deadline.after(60.0)

state = port.change(pull, deadline=deadline)
failed = [check.name for check in port.checks(pull, deadline=deadline) if check.conclusion == "failure"]
if port.capabilities(deadline=deadline).merge_queue:
    entry = port.enqueue(pull, deadline=deadline)
health = port.queue_health(deadline=deadline)
if isinstance(health, Unsupported):
    merged = port.merge(pull, deadline=deadline)
port.comment(pull, "Queued by the tick.", deadline=deadline)
releases = [tag.name for tag in port.tags(deadline=deadline) if tag.name.startswith("v")]
```

| Operation | GitHub call | Returns |
|---|---|---|
| `change(reference)` | REST `repos/<repository>/pulls/<number>` | `ChangeState` |
| `checks(reference)` | `change`, then REST `repos/<repository>/commits/<head>/check-runs` | `tuple[CheckState, ...]` |
| `runs(reference)` | `change`, then REST `repos/<repository>/actions/runs?head_sha=<head>` | `tuple[Run, ...]` |
| `enqueue(reference)` | GraphQL read of the base branch merge queue and the pull request id and head, then the `enqueuePullRequest` mutation | `QueueEntry`, or `Unsupported("enqueue")` |
| `merge(reference)` | `change`, then REST `PUT repos/<repository>/pulls/<number>/merge` with `sha=<head>` and `merge_method=squash` | `Merged` |
| `queue_health()` | GraphQL `mergeQueue(branch: <base>)` with its first 100 entries | `QueueHealth`, or `Unsupported("queue_health")` |
| `tags()` | `git ls-remote --tags origin` in `root` | `tuple[Tag, ...]` |
| `comment(reference, body)` | REST `POST repos/<repository>/issues/<number>/comments` | `Comment` |
| `capabilities()` | The same GraphQL queue read as `queue_health` | `Capabilities` |

- Every `gh` call goes through `scripts.swarm.github_reads.rest` with the caller name `tick.source_control`
  (`github.CALLER`), so each request is charged to that caller in `github-reads/charged.jsonl` under the AgentiHooks
  home, beside every other audited GitHub read.
- Each `gh` and `git` call gets `deadline.remaining` as its subprocess timeout. `checks`, `runs` and `merge` make a
  `change` read first, and each later call receives what is left of the same deadline.
- `checks` and `runs` read 100 rows a page and stop at the first page shorter than 100, or after ten pages, so at most
  1,000 rows come back.
- `enqueue` passes the head it just read as `expectedHeadOid`, and `merge` passes the head from its `change` read as
  `sha`. GitHub refuses both when the pull request head moved after the read, so a push between the read and the
  write is never queued or merged unread.
- A repository without a merge queue on `base` answers `Unsupported("enqueue")` and `Unsupported("queue_health")`
  instead of raising, and `capabilities()` reports `merge_queue=False`. `enqueue` then makes no second GraphQL call.
- `queue_health().depth` is the queue's total count; `states` holds the states of the first 100 entries only.
- `tags` drops the peeled `^{}` lines of annotated tags and strips `refs/tags/`, so each tag appears once with the
  commit `ls-remote` lists first: the tag object for an annotated tag, the commit for a lightweight one.
- `enqueue`, `queue_health` and `capabilities` use `base` for the queue branch, whatever branch the pull request
  targets.
- Provider errors are not caught. A non zero `gh` or `git` exit raises `subprocess.CalledProcessError` with the
  command's stderr, a call that outlives its timeout raises `subprocess.TimeoutExpired`, and an answer missing an
  expected field raises `KeyError`.

`scripts.swarm.stack.change(url, refused)` is the first caller outside the tests. It builds the port from a GitHub
pull request or issue address with `base` `dev` and the current folder as `root`, reads the change with a sixty second
deadline, and turns any of the errors above into a `SwarmError` that starts with `refused`. `swarm park` reads
through it whether each dependency pull request merged into dev when it picks the stacked base, and `swarm publish`
reads whether the task's recorded pull request is closed before it lets a worktree on a new branch publish for the
task.

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
| `close(item_id, outcome, detail="")` | `store.close(item_id, sender, outcome, detail)` | The closed item |

`send` passes no `ref`, `fyi`, `task` or `item_id`. `close` takes an outcome, one of `done`, `handoff`, `blocked` or
`cancel`, and its detail, and closes with the adapter's sender as closer. `handoff` needs the address the work went to
and `blocked` needs what it is blocked on. A bare `done` is accepted only for an item sent with `fyi`; any other item
raises `InboxError("done needs an outcome naming where the work went")`, and closing an already closed item raises
`InboxError("message <id> is closed: <reason>")`. The sender must also act for the item's address. `FakeInbox`
refuses an unknown outcome, a missing detail and a bare `done` on an item not listed in its `information` set.

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

## One lease heartbeat per tick pass

`scripts.swarm.lease.held_pass(store, slug, held)` holds the controller lease for one tick pass. `run_tick` enters it
first, before the `before` group, and closes it in its `finally` block; the master placement path under the tick lock,
`_under_tick_lock` in `scripts.swarm.tick`, holds one around its work too. On entry it renews the lease once, then
runs one `lease.kept` heartbeat thread until the pass ends. The renewal rules, the heartbeat interval and the renewal
before each step are in [Controller lease during long steps](../pillars/swarm.md#controller-lease-during-long-steps).

| Pass member | Behavior |
|---|---|
| `lease.PASS` | Holds the store, the swarm and the held epoch for the pass, so ledger writes resolve their `controller_epoch` through `epoch_for`, described under [Ledger adapter](#ledger-adapter). Reset when the pass ends, even when it raises. |
| `FencedRuntime.spawn`, `collect`, `expire` | Call `lease.require_epoch`, one lease read with no renewal, and raise `SwarmError("the controller lease is stale")` when another controller now holds the lease. |
| `FencedRuntime.has_capacity` | Applies the deployment mode refusal, then asks the wrapped runtime's `has_capacity` for a local deployment; it does not touch the lease. |
| `FencedRuntime.launch_room` | Reads the room left for launches; it does not touch the lease. |
| Every other `FencedRuntime` attribute | Forwarded to the wrapped runtime, such as `herdr_runtime`, `original_runtime` and `hive_launch`, with no renewal, fencing or thread. |
| The ledger client | Used as given; no wrapper renews the lease on ledger calls. |

The stopped swarm path, `_stopped_tick` in `scripts.swarm.cli`, uses the same `FencedRuntime` and the plain ledger
client.

`retire` and `relaunch` on the runtime are not checked against the lease epoch. A takeover during a step that makes
several of those calls is caught at the next step boundary, where `keep_tick` renews and raises.

Enter `held_pass` only around work that runs under `held`, a lease this controller acquired for the swarm:

```python
from scripts.swarm import lease
from scripts.swarm.commands import hive_id
from scripts.swarm.store import connect

store, slug = connect(), "rig-grade-swarm"
held = lease.acquire(store, slug, hive_id())
if held is not None:
    with lease.held_pass(store, slug, held):
        assert lease.epoch_for(slug) == held.epoch
```

## Declared effects

`Tools` in `scripts.tick.ports` holds one adapter per port. `Tools.for_effects(effects)` returns a copy whose ports
allow read operations always and write operations only on the ports named in `effects`. In this example
`source_control`, `runtime` and `processes` are the adapters, and `outbox` and `PASSES` the metrics values, built in
their sections above.

```python
import os

from scripts.inbox.store import connect as inbox_connect
from scripts.swarm.commands import hive_id
from scripts.swarm.store import connect as swarm_connect
from scripts.tick.adapters.coordination import StoreCoordination
from scripts.tick.adapters.host import local_host
from scripts.tick.adapters.inbox import StoreInbox
from scripts.tick.adapters.ledger import ClientLedger, resource_client
from scripts.tick.adapters.llm import ApiLlm
from scripts.tick.adapters.metrics import SpoolMetrics
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Tools

tools = Tools(
    ledger=ClientLedger("rig-grade-swarm", resource_client("rig-grade-swarm", service=True)),
    coordination=StoreCoordination(swarm_connect(), "rig-grade-swarm", hive_id()),
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
| `ledger` | `export` | `mutate` |
| `coordination` | `read`, `capacity` | `lease`, `lock`, `configure`, `mark` |
| `source_control` | `change`, `checks`, `runs`, `queue_health`, `tags`, `capabilities` | `enqueue`, `merge`, `comment` |
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

`scripts.tick.adapters.fakes` has `FakeLedger`, `FakeCoordination`, `FakeSourceControl`, `FakeInbox`, `FakeLlm`,
`FakeMetrics`, `FakeHost`, `FakeRuntime` and `FakeProcesses`. Each records a `Call` with the
operation, its arguments and the deadline, and answers from its `answers` dictionary keyed by operation. Each refuses
an expired deadline like the adapters. `fake_tools(effects)` builds a full `Tools` of fakes, restricted to `effects`
when given. In `fake_tools`, the ledger fake answers `export` with an empty dictionary, the processes fake answers
`scan` with an empty tuple and the source control fake has no merge queue. `FakeLedger` and `FakeCoordination` answer
`None` for any operation without an answer; they stamp no epoch and hold no lock, so a test of epoch or lock
behavior uses the real adapters.

Ledger and coordination fakes answer from their dictionaries:

```python
from scripts.tick.adapters.fakes import FakeCoordination, FakeLedger
from scripts.tick.deadline import Deadline

ledger = FakeLedger({"export": {"tasks": []}, "mutate": {"applied": 1}})
coordination = FakeCoordination({"lock": "token-1", "capacity": {"home": 2}})
deadline = Deadline.after(5.0)

assert ledger.mutate([{"op": "add"}], deadline=deadline) == {"applied": 1}
assert coordination.lock("placement", deadline=deadline) == "token-1"
assert [(call.operation, call.args) for call in coordination.calls] == [("lock", ("placement",))]
```

The other fakes are built the same way:

```python
from scripts.tick.adapters.fakes import FakeHost, FakeRuntime, fake_tools

tools = fake_tools(frozenset({"host"}))
host = FakeHost({"run": True})
runtime = FakeRuntime({"panes": [{"pane_id": "w1:p1"}]})
```

`FakeSourceControl(answers, merge_queue=False)` answers with records from `scripts.tick.ports.source_control`. Without
an answer for an operation it falls back as follows:

| Operation | No merge queue (default) | `merge_queue=True` |
|---|---|---|
| `enqueue` | `Unsupported("enqueue")` | `QueueEntry(reference, 1)` |
| `queue_health` | `Unsupported("queue_health")` | `QueueHealth(0, ())` |
| `capabilities` | `Capabilities(merge_queue=False)` | `Capabilities(merge_queue=True)` |
| `checks`, `runs`, `tags` | `()` | `()` |
| `change`, `merge`, `comment` | `None` | `None` |

An entry in `answers` always wins, with or without a queue. A step that must work on a provider without a merge
queue is tested against the default fake; give `change`, `merge` and `comment` an answer whenever the step reads one.

```python
from scripts.tick.adapters.fakes import FakeSourceControl
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Unsupported
from scripts.tick.ports.source_control import ChangeRef, ChangeState

pull = ChangeRef("owner/project", 7)
open_pull = ChangeState(pull, "open", "abc123", "dev", False, "https://github.com/owner/project/pull/7")
port = FakeSourceControl({"change": open_pull})
deadline = Deadline.after(5.0)

assert port.change(pull, deadline=deadline) is open_pull
assert port.enqueue(pull, deadline=deadline) == Unsupported("enqueue")
assert [call.operation for call in port.calls] == ["change", "enqueue"]
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

The ledger and coordination delegates, `scripts.swarm_ledger.api.client`, `scripts.swarm.store`,
`scripts.swarm.lease` and `scripts.routing.reservation`, are not on the list, so the check does not catch a tick
module that imports them.

Any relative import in a scanned module also fails, whatever it names, since the check cannot resolve it.
The failure lists each module path with the names it imported. Move the call behind a port operation in the adapter
and call the port from the step.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Unsupported("enqueue")` or `Unsupported("queue_health")` from source control | The repository has no merge queue on the adapter's `base` branch, or the fake was built without one. | Check `capabilities().merge_queue` first and merge with `merge` instead, or build the adapter with the branch that has the queue. |
| `CalledProcessError` from source control `merge` or `enqueue` | GitHub refused the write; a head that moved after the adapter's read is the usual cause. | Read the change again and retry against the new head once its checks pass. |
| `CalledProcessError` from source control `tags` | `git ls-remote` failed in `root`, for example no `origin` remote or no access. | Run `git ls-remote --tags origin` in that folder. |
| `TimeoutExpired` from a source control operation | `gh` or `git` outlived what was left of the deadline. | Give the step a larger deadline; `checks`, `runs` and `merge` spend it on two or more calls. |
| `TimeoutError: tool deadline expired` | The deadline passed before the call; no outside system was touched. | Give the step a larger budget or check `deadline.remaining` before a slow call. |
| `UndeclaredEffect: <port> is not a declared effect` | A step called a write operation on a port missing from its effects. | Declare the port in the step's effects. |
| `TypeError: tools require a Deadline keyword argument` | A restricted port was called without `deadline=`. | Pass the step's `Deadline` by keyword. |
| `KeyError: 'unknown metrics table: <name>'` | `emit` named a table the adapter was not built with. | Add the `Table` to the `tables` mapping. |
| `BackendFailure: <model>: timeout` from `classify` | The decisions API did not answer within `deadline.remaining`. | Raise the deadline, or build `ApiLlm` with a model that answers faster. |
| `InboxError: done needs an outcome naming where the work went` from `inbox.close` | The item was not sent with `fyi`, and the step closed it as `done` with no detail. | Pass the detail naming where the work went, or close with `handoff`, `blocked` or `cancel`. |
| `Outcome(..., Status.UNAVAILABLE, "", detail="no agent named <handle>")` from `runtime.inspect` or `runtime.stop` | The `agents` lookup found no record for that name. | Check the name against the swarm's agent records; the router was not called. |
| `TypeError` from `runtime.launch` | The request mapping lacks one of `config`, `lane`, `name`, `task`, or carries another key. | Pass exactly those four keys. |
| `runtime.panes` returns `[]` | herdr answered without a `panes` list. | Run `herdr pane list` on the host and check the herdr server is up. |
| A reaped or retired process was not found in a tick pass | The process exited before selection, so its `/proc` entry was gone and the snapshot was kept. | No action; the process is already gone. |
| HTTP 409 `stale_controller`, `the controller lease is stale`, on `ledger.mutate` or a `LedgerClient` write | The operation carried a `controller_epoch` that is no longer the live lease of that ledger: another controller took over. | No action in the step. The pass stops at its next step boundary, where `keep_tick` raises, and a later pass acquires the lease again once no other controller holds it. |
| `SwarmError: the controller lease is stale` at the start of a pass or before a launch | `held_pass` renewal or `require_epoch` found another controller's lease. | Check which hive holds the swarm's lease; a pass on this controller resumes only once it acquires the lease. |
| `coordination.lease` returns `None` | Another owner holds a live lease, or the swarm's controller host names another owner. | Run the step on the controller that holds the lease. |
| `coordination.lock` returns `None` | Another holder keeps the named lock until its expiry. | Skip the guarded work this pass; the lock expires with the holder's deadline. |
| `SwarmError` from `coordination.configure` | A changed field failed the store's checks, such as an unknown `state` or `autonomy`. | Pass a value the store accepts. |
| `ledger.export` or `ledger.mutate` runs past its deadline | The deadline bounds each HTTP request, and a call can make several. | Size the deadline for the retries, or check `deadline.remaining` between calls. |
| `Unsupported(name)` from `host.read` or `host.run` | The host has no reading or job by that name. | Add it to the `JobHost` mappings or handle the missing capability in the step. |
| `test_no_tick_step_module_imports_an_adapted_implementation` fails | A tick module imports one of the adapted implementations. | Route the call through the matching adapter. |
