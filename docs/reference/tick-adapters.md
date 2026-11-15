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
| `SourceControl` | `github.GitHubSourceControl` | `scripts.swarm.github_reads.rest` (`gh api`) and `git ls-remote` | `change`, `checks`, `runs`, `queue_health`, `tags`, `capabilities`, `issue_state`, `rate_limit`, `issues_enabled`, `latest_runs`, `merged_at`, `open_changes`, `workflow_runs`, `download`, `graph` / `enqueue`, `merge`, `comment`, `close_issue`, `cancel_run` |
| `Inbox` | `inbox.StoreInbox` | `scripts.inbox.store.InboxStore` | `read` / `send`, `close` |
| `Llm` | `llm.ApiLlm` | `hooks.classifier.api.DecisionsApiBackend` | none / `classify` |
| `Metrics` | `metrics.SpoolMetrics` | `scripts.swarm.metrics_outbox` spool and tables | none / `emit` |
| `Host` | `host.JobHost`, built by `host.local_host` | `scripts.swarm.host_budget`, `refresh_job`, `snapshot_job` | `read` / `run` |
| `Runtime` | `runtime.RouterRuntime` | `scripts.swarm_v2.runtime.base.RuntimeRouter` and herdr `pane list` | `inspect`, `panes` / `launch`, `stop` |
| `Processes` | `processes.PassProcesses` | `hooks.proc.processes` | `scan` / none |

The adapters exist and are tested; no tick pass builds a `Tools` bundle from them yet. Steps receive them once the
tick engine passes a `Tools` bundle to each step. Two adapters are already in use outside a bundle: every swarm tick
pass uses `PassProcesses` for the reaper's process scan, described under
[One process scan per tick pass](#one-process-scan-per-tick-pass), and the swarm commands listed under
[Swarm callers on the port](#swarm-callers-on-the-port) reach GitHub through `GitHubSourceControl`. A Semgrep rule
keeps new `gh` commands out of swarm code, described under [The gate on gh commands](#the-gate-on-gh-commands). The ledger
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

[Pull request 9720](https://github.com/the-cloud-clockwork/agentihooks/pull/9720) later removed the `after` site and
the `wrapper status` group. Last tick, heartbeat and status publish now run in the `closing` group, which `run_tick`
calls from its `finally` on every ending; see [The closing lane](tick-engine-steps.md#the-closing-lane).

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

Every adapter method that takes a `deadline` keyword calls `deadline.live()` in its own body before it touches
Redis, the network, SQLite, git, herdr or a job, so all nine adapters refuse an expired deadline with the same
message. A method that calls another method or helper still makes its own check first, so a read through
`graph` is refused before it is charged to the caller's budget. Where a call takes a timeout, the adapter
passes the seconds `live()` returned: `gh api` and `git ls-remote` in the source control adapter, the decisions API
in the llm adapter, and the ledger client's request timeout, which is the smaller of its own timeout and those seconds.

The fenced records adapter, `records_redis.RedisRecords`, takes no deadline. Its writes rely on the fence's lease and
revision checks and their retries.

`scripts.swarm.fleet_red.read(repo, ports, deadline=...)` takes the caller's deadline and gives `open_changes` a
child of it capped at `fleet_red.SECONDS` (60), so the listing ends at whichever comes first. Without a caller
deadline it gives `open_changes` a fresh 60 second deadline.

The unit test `tests/tick/test_adapter_deadlines.py` walks every module in `scripts/tick/adapters` except `fakes.py`
by its syntax tree and fails on a method with a keyword-only `deadline` argument whose first statement, after its
docstring and local imports, does not call `deadline.live()`. It prints the methods and checks it counted per
module, and a planted method without a check is named in the failure. The same file asserts that the records adapter
takes no deadline and that every source control, runtime, inbox, host, metrics, llm and processes method refuses an
expired deadline before touching its outside system. The ledger and coordination adapters keep their own expiry tests.

### Writing an adapter method with a deadline

Take the deadline as a keyword-only argument named `deadline`, and make the first statement after the docstring and
local imports call `deadline.live()`. Keep the seconds it returns when the outside call takes a timeout.

```python
def issue_state(self, reference: ChangeRef, *, deadline: Deadline) -> str:
    """Read whether the issue is open or closed."""
    deadline.live()
    return self._api([f"repos/{reference.repository}/issues/{reference.number}"], deadline)["state"]


def tags(self, *, deadline: Deadline) -> tuple[Tag, ...]:
    """List the remote tags with the commits they name."""
    timeout = deadline.live()
    result = self.git(
        ["git", "ls-remote", "--tags", "origin"],
        cwd=self.root,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=True,
    )
    rows = [line.partition("\t") for line in result.stdout.splitlines() if line]
    return tuple(Tag(ref.removeprefix("refs/tags/"), sha) for sha, _, ref in rows if not ref.endswith("^{}"))
```

The walk counts any call to `deadline.live()` inside the first statement, so `timeout = deadline.live()`,
`self.clients(deadline.live())` and `max(1, int(deadline.live() * 1000))` all pass. It grades only `def` methods
with a keyword-only argument named `deadline`; a check on another name such as `budget.live()`, or a check placed
after other work, is reported as missing. A caller that needs a shorter bound for one outside call passes
`deadline.child(seconds)`, as `fleet_red.read` does, so the call ends at the earlier of the two expiries.

The walk runs in the unit suite in CI. Local test runs are blocked by default, so push and read the result on the
pull request. The walk is skipped inside a mutation run, where `MUTANT_UNDER_TEST` is set, because the mutated copy
rewrites every method body. It prints one line per module, which shows in the failure message or with `pytest -s`:

```text
coordination: 6 methods, 6 checks
github: 20 methods, 20 checks
host: 2 methods, 2 checks
inbox: 3 methods, 3 checks
ledger: 2 methods, 2 checks
llm: 1 methods, 1 checks
metrics: 1 methods, 1 checks
processes: 2 methods, 2 checks
records_redis: 0 methods, 0 checks
runtime: 4 methods, 4 checks
```

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
  the lock expires by itself when the deadline does. Give the call a deadline sized to the work the lock guards. The
  adapter has no release operation; the lock ends at its expiry.
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
with the same twenty operations, and no step changes.

| Record | Fields | Meaning |
|---|---|---|
| `ChangeRef` | `repository`, `number` | One change, such as a pull request: `"owner/name"` and its number. |
| `ChangeState` | `reference`, `state`, `head`, `base`, `merged`, `url`, `head_branch`, `body`, `draft`, `head_repository` | The change's provider state (`open` or `closed` on GitHub), head commit, target branch, whether it merged, its web address, the branch it was opened from, its description, whether it is a draft and the `owner/name` of the repository holding its head branch. `head_branch`, `body` and `head_repository` default to `""` and `draft` to `False` for records built without them; `head_repository` is `""` when the head repository was deleted. |
| `CheckState` | `name`, `status`, `conclusion`, `url` | One check on the change's head. `conclusion` is `None` until it completes. |
| `Run` | `id`, `name`, `head`, `status`, `conclusion`, `url` | One workflow run on the change's head. |
| `RepositoryRun` | `id`, `branch`, `event`, `status` | One recent workflow run of the repository: its head branch, the event that started it (`merge_group` for a merge queue run) and its status. |
| `WorkflowRun` | `id`, `event`, `status`, `conclusion`, `head_branch`, `head_sha`, `created_at`, `run_started_at`, `updated_at`, `run_attempt` | One run of a named workflow with its outcome and timing, as `workflow_runs` lists it. |
| `OpenChange` | `url`, `checks` | One open pull request and the `RollupCheck` records of its newest commit. |
| `RollupCheck` | `name`, `result`, `at` | One check in that rollup: a check run's name, conclusion and finish time (its start while it runs), or a commit status's context, state and creation time. |
| `RateLimit` | `used`, `remaining`, `reset` | The login's core REST quota: calls spent, calls left and the reset time in epoch seconds. |
| `QueueEntry` | `reference`, `position` | The change's position in the merge queue after `enqueue`. |
| `QueueHealth` | `depth`, `states` | The number of queue entries and the state of each entry read. |
| `Merged` | `reference`, `sha` | The commit a merge produced. |
| `Tag` | `name`, `sha` | One remote tag and the object `git ls-remote` lists for it. |
| `Comment` | `reference`, `url` | The web address of a posted comment. |
| `Capabilities` | `merge_queue` | Whether the provider has a merge queue on the base branch. |

`GitHubSourceControl(repository, root, base, git=subprocess.run, gh=None)` is a frozen dataclass:

| Argument | Meaning |
|---|---|
| `repository` | `"owner/name"`, the repository whose merge queue `queue_health` and `capabilities` read. |
| `root` | The working folder for every `gh` and `git` call; `tags` reads the `origin` remote of this checkout. |
| `base` | The branch whose merge queue `enqueue`, `queue_health` and `capabilities` use, normally `dev`. |
| `git` | The callable that runs `git`, with the `subprocess.run` signature. Tests pass a recorder here. |
| `gh` | The callable `download` and `graph` run `gh` through, with the `subprocess.run` signature; `None` uses `subprocess.run`. `scripts.swarm.merge_queue` passes its own runner here. |

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
| `issue_state(reference)` | REST `repos/<repository>/issues/<number>` | The issue's state, `open` or `closed` |
| `close_issue(reference)` | REST `PATCH repos/<repository>/issues/<number>` with `state=closed` | The state GitHub reports after the write |
| `rate_limit()` | REST `rate_limit` | `RateLimit` for the core REST quota |
| `issues_enabled()` | REST `repos/<repository>` | `has_issues`, `True` when the repository takes issues |
| `latest_runs()` | REST `repos/<repository>/actions/runs?per_page=100`, one page | `tuple[RepositoryRun, ...]`, newest first |
| `cancel_run(run_id)` | REST `POST repos/<repository>/actions/runs/<run_id>/cancel` | `None` |
| `merged_at(commit)` | REST `repos/<repository>/commits/<commit>/pulls` | The `merged_at` stamp of the first listed pull request that merged, or `None` |
| `open_changes()` | GraphQL `pullRequests(states: OPEN, first: 25)` with the newest commit's `statusCheckRollup`, up to eight pages | `tuple[OpenChange, ...]`, at most 200 |
| `workflow_runs(workflow, event, conclusion, since)` | REST `repos/<repository>/actions/workflows/<workflow>/runs?event=<event>&status=<conclusion>&exclude_pull_requests=true&created=>=<since>`, every page up to ten | `tuple[WorkflowRun, ...]` |
| `download(path)` | REST `<path>`, the body kept as bytes | `bytes` |
| `graph(document, variables)` | GraphQL `document` with each variable passed as a string | The whole decoded answer, `errors` included |

`download` and `graph` are charged to the reader in force, `github_reads.CALLER`, instead of `tick.source_control`,
so a caller wraps them in `github_reads.calling("<reader>")` to keep its own line in the audit. Both run `gh` through
the adapter's `gh` field when it is set, else `subprocess.run`. `download` raises `CalledProcessError` on a non zero
exit with the raw output kept in `stdout`. `graph` also raises `CalledProcessError` on a refused document; GitHub's
JSON body, with its `errors`, stays in `stdout` for callers that read a partial answer. `graph` runs any document,
mutations included, yet `Tools` lists it as a read, so a step restricted to reads can still pass it a mutation:
treat a mutation through `graph` as a write in review.

For the issue operations, `ChangeRef.number` is the issue number. `rate_limit` reads the login, not a repository, so
an adapter built with an empty `repository` answers it. GitHub does not charge the `rate_limit` read against the
quota it reports.

```python
from pathlib import Path

from scripts.tick.adapters.github import GitHubSourceControl
from scripts.tick.deadline import Deadline
from scripts.tick.ports.source_control import ChangeRef

port = GitHubSourceControl("the-cloud-clockwork/agentihooks", Path.cwd(), "dev")
deadline = Deadline.after(60.0)
issue = ChangeRef("the-cloud-clockwork/agentihooks", 9132)

if port.issues_enabled(deadline=deadline) and port.issue_state(issue, deadline=deadline) == "open":
    port.close_issue(issue, deadline=deadline)
quota = GitHubSourceControl("", Path.cwd(), "dev").rate_limit(deadline=deadline)
for run in port.latest_runs(deadline=deadline):
    if run.status == "queued" and run.event != "merge_group":
        port.cancel_run(run.id, deadline=deadline)
```

On a branch with a merge queue a caller lands a change with `enqueue`, never with `merge`: `merge` squashes straight
into the branch and skips the queue's checks. Call `merge` only when `capabilities().merge_queue` is `False`.

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

## Swarm callers on the port

Swarm code builds the port through these builders, always with `base` `dev`:

| Builder | Repository | Used for |
|---|---|---|
| `stack.source_control(url)` | The owner and name in a `https://github.com/<owner>/<name>/pull/<n>` or `/issues/<n>` address; returns the port and the `ChangeRef` | Reads and writes on one pull request or issue |
| `stack.repository_source_control()` | The repository the checkout's `origin` remote names | `issues_enabled` |
| `stack.origin_source_control(root)` | The repository the `origin` remote of the checkout at `root` names, with `root` as the working folder | `merged_at`, `workflow_runs` |
| `stack.login_source_control()` | None (`""`) | `rate_limit`, `download`, `graph` |
| `panic.port(repository)` | The fleet repository named | `latest_runs`, `cancel_run`, `open_changes` |
| `merge_queue.READER` with the caller's `gh` runner | None (`""`) | `graph`, `download` |

Every builder except `stack.origin_source_control` uses the current folder as `root`.

`stack.source_control` raises `ValueError` for an address outside `https://github.com/`.
`stack.repository_source_control` raises `SwarmError("cannot read the repo origin: <stderr>")` when
`git remote get-url origin` fails.

| Command | Port operations | Deadline | On a failed call |
|---|---|---|---|
| `swarm park` | `change` on each dependency pull request, to tell whether it merged into dev before picking the stacked base | 60 s each | `SwarmError` naming the read |
| `swarm park`, task without a recorded issue | `issues_enabled` on the origin repository; `True` refuses the park with `the repo has issues; open one and run swarm issue before parking` | 60 s | `SwarmError("cannot tell whether the repo has issues: <detail>")` |
| `swarm park`, task with a recorded issue | `comment` on that issue with the parked branch, its dependencies and the stacked base, posted after the ledger note | 60 s | `SwarmError("could not comment on the issue: <detail>")` |
| `swarm publish` | `change` on the task's recorded pull request: whether it is closed, and whether it merged from the task's own branch (`head_branch`) | 60 s | `SwarmError` naming the read |
| `swarm ready`, closing settled issues | `issue_state`, then `close_issue` only when it is `open`, then `comment` linking the merged pull request | 60 s each | An action line `could not close issue <url>: <detail>` or `closed issue <url>; could not comment on it: <detail>`; the ready step goes on |
| GitHub rate limit sampler | `rate_limit` on the login | 30 s | The error propagates to the sampler |
| `swarm ready` | `change` on the recorded pull request; `draft` refuses with `pull request <url> is a draft; mark it ready for review with gh pr ready first` | 60 s | `SwarmError("cannot read whether pull request <url> is a draft; run swarm ready again when it answers")` |
| `swarm publish`, recording the branch | `change` on the recorded pull request for `head_branch` and `head_repository` (`cli.pull_branch`) | 20 s | Both values read as empty and the publish goes on |
| Swarm health checks | `checks` on a task's pull request head | 20 s | The error propagates to the health check |
| Fleet red notice | `open_changes` on each repository with an open task pull request | 60 s | The listing cache keeps the failure for its ten minute window |
| Ledger host facts | `merged_at` on the newest dev commit of the ledger checkout | 10 s | `None`; the age falls back to the reflog stamp |
| CI speed sampler | `workflow_runs` on `test.yml` for one event, once per conclusion `success` and `failure`, in parallel | 60 s each | The error propagates to the sampler |
| Mutation wait | `download` on a run or job endpoint under the caller's reader; `rate_limit` when GitHub answers with a rate limit, to time the backoff | 20 s each | `Unreachable("gh api <endpoint>: <stderr>")`, or `RateLimited` with the hold time on a rate limit |
| Pull request check views (`ledger_events.view` and the batched checks reader) | `graph` under the readers `swarm.checks.fresh` and `swarm.checks` | 20 s | `None` or an empty answer; a rate limit starts the mutation wait backoff |
| `swarm merge queue` and `swarm merge priority` | `graph` for the pull request state and the `enqueuePullRequest`, `dequeuePullRequest` or `mergePullRequest` mutation, `download` for REST reads | 20 s each | `SwarmError` carrying GitHub's message |
| Panic cancel in the tick | `change` on each `--fix` pull request for its `head_branch`, then per fleet repository `latest_runs` and `cancel_run` on every run in `queued`, `in_progress`, `waiting`, `pending` or `requested` that is neither a `merge_group` run nor on a fix branch | 60 s each | The pass reports `panic cancel failed: <error>` and retries a minute later |

`swarm merge queue` lands a pull request with `enqueuePullRequest` whenever the repository has a merge queue on dev,
and squashes it with `mergePullRequest` only when GraphQL reports none; the port's own `merge` is not used there.

`<detail>` is the provider's stderr for a failed `gh` call (`stack.refusal`), else the exception text. `swarm park`,
`swarm publish`, `swarm ready`, the fleet red notice and the ledger host facts catch `OSError`,
`SubprocessError`, `TimeoutError`, `ValueError` and `KeyError` (`stack.PORT_ERRORS`), so an expired deadline or an
answer missing a field reads as a refusal instead of a traceback.

## The gate on gh commands

Swarm and tick code reach GitHub through the port. The Semgrep rule `swarm-source-control-port` in
`.semgrep/source-control.yml` fails the scan on either form of a `gh` command in a Python file under `scripts/swarm` or
`scripts/tick`:

- a list literal starting with `"gh"`, such as `["gh", "pr", "view", url]` or `["gh", *args]`;
- a call to `scripts.tick.adapters.github.command`, the adapter's `gh` command builder, under any import alias.

The rule's `paths.include` lists `/scripts/swarm/**`, `/scripts/tick/**` and `*.test.py`; a unit test in
`tests/test_ci_semgrep.py` pins that list. Every tick module falls under the rule, step modules, ports and adapters
alike, except the GitHub adapter, which stays on the exception list below. Tick code reads source control through a
`SourceControl` port, such as `Tools.source_control`:

```python
import subprocess

from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import SourceControl
from scripts.tick.ports.source_control import ChangeRef, ChangeState


def state_by_gh(number: int) -> str:
    return subprocess.run(["gh", "pr", "view", str(number), "--json", "state"], capture_output=True, text=True).stdout


def state_by_port(port: SourceControl, number: int, deadline: Deadline) -> ChangeState:
    return port.change(ChangeRef("the-cloud-clockwork/agentihooks", number), deadline=deadline)
```

In a module under `scripts/tick`, the first function fails the scan and the second passes. A finding opens with the
file and the rule; the scan then prints the rule message and the matching line:

```text
scripts/tick/<module>.py
❯❯❱ semgrep.swarm-source-control-port
```

`git` commands are not matched. `.semgrep/source-control.test.py` holds the failing and passing fixtures, a tick step
that builds a `gh` list and one that reads through the port included, graded with `semgrep --test` in the same
workflow step that scans the code; the rule also includes `*.test.py` so that fixture file is scanned. The fixture
grades the pattern shapes; the repository scan in the same step grades the path include.

Files that still build `gh` commands are listed twice and must match: in the rule's `paths.exclude` and in
`.semgrep/source-control-allowlist.json`. The list only shrinks. The Semgrep workflow step
`Rule exceptions only shrink` reads every `.semgrep/*-allowlist.json` from the base commit and fails with
`<list> exceptions grew: [<paths>]` when a head list holds a path its base list lacks; otherwise it prints
`<list> exceptions: <head count> of <base count>`. A base without a list passes with
`The base holds no <list>; the head list starts it`. The same step grades
`.semgrep/tick-unbounded-read-allowlist.json`, the modules the `tick-unbounded-store-read` rule still excludes, each
mapped to the task that removes its whole hash reads or unpaged key scans. A unit test in
`tests/test_ci_semgrep.py` requires every entry to be one `.py` path with no wildcard, and names files that have left
the list, such as `scripts/swarm/stack.py`, so they cannot return.

The list holds two files: `scripts/tick/adapters/github.py`, the adapter, and `scripts/swarm/github_reads.py`, the
audited runner the adapter calls, which builds each `gh` command with the adapter's `command`. Every swarm caller has
moved onto port tools; the unit test pins the list to exactly those two paths and names each moved file, so none can
return. The `swarm-tick-source-control-port`
entry in `feature-map.json` names the port and its GitHub adapter as the seam, with the tests that prove the adapter
builds the only `gh` command line.

To move a file off the list:

1. Add the operation the file needs to `SourceControl` in `scripts.tick.ports.contracts`, to `GitHubSourceControl`,
   and to `FakeSourceControl`, and list it as a read or write in `scripts.tick.ports.tools` so `Tools.for_effects`
   restricts it.
2. Call the port from the file through one of the builders above, with a `Deadline`, and turn `stack.PORT_ERRORS`
   into the command's own refusal.
3. Delete the file from both `paths.exclude` and `source-control-allowlist.json` in the same pull request.

A new `gh` command in a listed file is not caught by the rule; review it as a move in the wrong direction. A new file
with a `gh` command fails the scan, and adding it to the list fails the shrink step.

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
| `live_names(listing)` | Calls `listing()` on the first read, keeps that set and returns a new copy of it on every later read. |
| `forget_names()` | Drops the held live names set, so the next `live_names` call lists again. |

`table is again` holds. Build a new `PassProcesses` for each pass; one instance never refreshes by itself. `scan`
and `rescan` refuse an expired deadline before reading `/proc`; `live_names` and `forget_names` take no deadline and
read no process table.

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

## One live names listing per tick pass

`controller.FencedRuntime.live_names` returns the names of the swarm's live agents. On the herdr runtime that listing
walks `/proc` through `terminate_agent.sessions`, checks each managed agent's server, and adds the hive's names, and
many tick steps read it: the agent checks in `scripts.swarm.tick`, resume, master launch and wake, master stall,
lifetime and status. Inside a
tick pass the fenced runtime lists once and every later read in the pass gets the same names.

| Read | Inside a tick pass | Outside a tick pass |
|---|---|---|
| `FencedRuntime.live_names()` | `PassProcesses.live_names(runtime.live_names)` on the pass's process model: the first read lists, later reads get a copy of that set | The wrapped runtime's `live_names()` on every call |
| The wrapped runtime's own `live_names()` | Lists on every call | The same |

- The listing is held on the `PassProcesses` in the `processes` port of `PASS_TOOLS`, which `run_tick` sets after
  `lease.held_pass` and resets when the pass ends, even when it raises; see
  [Effects in a tick pass](#effects-in-a-tick-pass). Every `FencedRuntime` built in that pass shares the one listing,
  and a change through one of them drops it for all.
- Each read returns a new `set`, so a caller that adds or removes a name does not change what the next read returns.
- A pass is any code running while `PASS_TOOLS` holds a `Tools` whose `processes` is a `PassProcesses`. With no pass
  tools, or with a `processes` port of another type such as a test fake, every call lists.

These fenced operations drop the listing when they return, and also when they raise, so the next read lists again
and sees the agent the call started or stopped:

| Operation | Where it is wrapped |
|---|---|
| `spawn` | Around the placed launch, after the lease epoch check; a lease refusal raises before the launch and keeps the listing |
| `collect`, `expire` | Around the hive launch's `collect` or `expire`, after the lease epoch check |
| `retire`, `reap_name`, `close_space`, `close_pane`, `resume` | Named in `controller.AGENT_CHANGES`; `__getattr__` forwards them to the wrapped runtime through `_changing` |

Any other forwarded attribute, such as `retire_pane`, `retire_process`, `original_runtime` or `herdr_runtime`, keeps
the listing. A call made on one of those, or on the wrapped runtime directly, is not seen until the pass ends or one of
the operations above runs.

```python
from scripts.swarm import controller, lease
from scripts.swarm.commands import hive_id
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import connect

store, slug = connect(), "rig-grade-swarm"
held = lease.acquire(store, slug, hive_id())
if held is not None:
    deployment = controller.deployment_for(store.config(slug), {})
    fenced = controller.FencedRuntime(store, slug, held, HerdrRuntime(), deployment)
    first = fenced.live_names()
    second = fenced.live_names()
```

Outside a pass, as in this example run from a shell, `first` and `second` each come from their own listing. Inside a
pass they come from one listing; a `fenced.retire(agent)` between them would make `second` list again.

A new step that stops or starts agents through a new runtime method drops the listing by adding the method name to
`AGENT_CHANGES`, or by calling the operation through `FencedRuntime._changing`, as `spawn`, `collect` and `expire` do:

```python
AGENT_CHANGES = frozenset(["retire", "reap_name", "close_space", "close_pane", "resume", "retire_pane"])
```

`tests/swarm/test_controller_live_names.py` holds the contract under the `fenced-live-names-listing` feature: one
listing per pass with copies served, a new pass lists again, every call lists outside a pass, each agent change drops
the listing, a spawn drops it whether it succeeds or fails, and a change through one fenced runtime drops the listing of
another. `test_running_pass_lists_live_names_once_plus_once_per_launch` in
`tests/swarm/tick_scenarios/test_characterisation.py` replays the running scenario through `run_tick` and expects three
listings for its three launches.

## One placement scan per placement pass

Nothing opens a placement pass since the tick spawn step was removed. `controller.FencedRuntime` keeps
`_local_slots_cache` empty, so every read in the table below behaves as its Outside a placement pass column says. A
caller that builds its own `local_slots.PassCache` and passes it to `local_slots.running`, `held` or `claim` still
reads once per cache.

`PassCache.once(read, *args)` returns the pass's result of `read(*args)`. The first call with a given function and
arguments runs the read under the cache's lock and keeps the value; every later call in that pass, from any launch
thread, gets the same value without reading again. A different function or different arguments is a separate entry.

| Read | Inside a placement pass | Outside a placement pass |
|---|---|---|
| Live session scan, `local_slots.live`, which walks `/proc` through `terminate_agent.sessions` | Once per pass, `cache.once(live)` | On every `running`, `held` and `claim` call |
| Swarm agent records of every swarm, `local_slots._scanned` | Once per pass, keyed by the store | On every call |
| The workstation host reading, `controller._host_reading(store, machine)`, which runs `headroom.measure` against the machine's last saved reading | Once per pass and machine | On every `_workstation_room` call |
| The local slot claims hash, `local_slots.key(machine)` | Read and written on every claim under `WATCH` | The same |

`FencedRuntime._local_refusal` passes the pass cache to `local_slots.claim`, so each claim counts the live workers
from the pass scan. The claim hash is never cached: every claim still reads it under `WATCH` and retries on a
`WatchError`, so concurrent launches in one pass cannot take more than `local-max-agents` slots between them.
`local_slots.occupancy`, read by the hive launch, the swarm probe and the spawn stall health check, takes no cache and scans each time.

Before this cache, each launch walked `/proc` three times, once each through `running`, `held` and `claim`, so five
concurrent launches in one pass ran fifteen walks at once. `agentihooks swarm rig-grade-swarm tick-report --passes 60`, run on
the workstation tick before pull request 9630 merged on 4 November 2026, put the fenced runtime spawn at a median of
10.9 seconds and a p95 of 15.4 seconds per launch. Run the same command after a change to compare.

A value read once holds for the whole pass. A session that starts or ends during the pass, or a host reading that
changes, is seen by the next placement pass. The claim hash still bounds the slots taken.

Use the cache for another read that every launch of a pass repeats:

```python
from scripts.swarm import local_slots


def placed(store, slug, tasks):
    cache = local_slots.PassCache()
    return [local_slots.running(store, slug, task, cache) for task in tasks]
```

`tests.hive.test_fenced_spawn_pass` holds the cache contract: a pass cache reads each value once, and a claim given
the cache counts live workers from its one scan.

### Timed rows inside a fenced spawn

`FencedRuntime._spawn` records two timed rows through `timing.step` for every launch, inside the spawn step's own
row:

| Step row | Times |
|---|---|
| `scripts.swarm.controller.FencedRuntime._refusal` | The placement refusal: the deployment mode refusal, then either the local refusal (workstation room and the local slot claim) or the hive and backend refusal, which on a hive placement can reach the workstation claim |
| `scripts.swarm.controller.FencedRuntime.runtime_spawn` | The wrapped runtime's `spawn`: the herdr pane, the Kubernetes worker or the hive launch |

Both rows pass `report=False`: a raised error still ends the row with outcome `error` and propagates, but it does not
reach the spawn stall failure hook, `timing.ON_FAILURE`, so one failed launch is recorded once, under the step that
called the spawn. The rows show in `swarm <id> tick-report` as their own step rows; see
[Tick report](tick-report.md#output).

```bash
agentihooks swarm <swarm slug> tick-report --passes 60 | grep -E 'spawn|FencedRuntime'
```

When `runtime_spawn` takes most of a slow `spawn` row, the wrapped runtime is the slow part; when `_refusal` does,
look at the placement reads in the table above.

### Timed rows inside spawn order

`_spawn_order` in `scripts.swarm.tick` builds the queue the spawn step launches from, and records its own row,
`scripts.tick.steps.place._spawn_order`. Its parts are timed through `timing.timed`, so each has a row of its own recorded
while `_spawn_order` runs. It first times `scripts.swarm.capacity.read`, `scripts.swarm.capacity.caps`, `scripts.swarm.dispatcher.claim_key`,
`scripts.swarm.preempt.yielding` and `scripts.swarm.tick.claim_view`. Then, for each lane in the caps, it times four
parts in this order:

| Step row | Runs | Times |
|---|---|---|
| `scripts.swarm.tick._running` | Once per lane | The running agent filter: the task rows of the lane's agents that have not ended and still own their task. Its length is the lane's busy count, which offsets the rank of every task queued for the lane |
| `scripts.swarm.tick.ready_in_order` | Once per lane | The lane's ready tasks in ledger or dispatched order |
| `scripts.swarm.readiness.launchable` | Once per lane | The ready tasks the tick launches under the swarm engine, narrowed to the capacity decision's tasks when it names any |
| `scripts.swarm.focus_budget.place` | Once per lane | Focus budget placement: which ready tasks fit the lane cap after the running ones, and which wait. It includes its two `profile_first.placeable` rows |

`focus_budget.place` locates tasks through `scripts.swarm.profile_first.placeable` twice per lane: once for the ready
tasks outside the focus queue, once for the focused ones. Each `placeable` row in turn holds one
`scripts.swarm.profile_first.decided` row and one `scripts.swarm.seat_spawn.placed_elsewhere` row for every task it
checks, until the lane's free room is filled. After the lanes, `scripts.tick.steps.place._ranked` sorts the whole queue
once, `scripts.swarm.focus_budget.ranks` reads each task's focus rank and `scripts.swarm.focus_budget.ordered` sorts
the queue by that rank. Every call in spawn order that reads the store, the runtime or the ledger document has a row;
only the local queue and wait list bookkeeping between them is untimed.

The code fixes the counts: `_running`, `ready_in_order`, `scripts.swarm.readiness.launchable` and
`focus_budget.place` each run once per lane per spawn order, and `placeable` twice, so over a window recorded entirely
after this change merged the first four have equal counts and `placeable` has twice that.

Read the rows with:

```bash
agentihooks swarm <swarm slug> tick-report --passes 60 \
  | grep -E 'placement_pass|capacity|claim_key|yielding|claim_view|_spawn_order|_running|ready_in_order|launchable|focus_budget|profile_first|placed_elsewhere|_ranked'
```

A snapshot of the spawn order rows from the workstation tick of `rig-grade-swarm`, read at 15:04 UTC on 4 November
2026 over a window that began before pull request 9673 merged, with three lanes:

```text
scripts.swarm.focus_budget.place  138  0.054s  1.585s  0.18%
scripts.swarm.profile_first.placeable  426  0.000s  0.311s  0.29%
scripts.tick.steps.place._ranked  71  0.002s  0.009s  0.00%
scripts.swarm.tick._running  138  0.000s  0.001s  0.00%
scripts.tick.steps.place._spawn_order  71  3.336s  17.861s  2.26%
scripts.swarm.tick.claim_view  71  0.050s  0.196s  0.02%
scripts.swarm.tick.ready_in_order  213  0.111s  2.357s  0.80%
```

The columns are count, median, p95 and CPU share; see [Tick report](tick-report.md#output). The window began before
the merge: its 71 spawn orders over three lanes gave 213 `ready_in_order` and 426 `placeable` rows, while `_running`
and `focus_budget.place`, which the merge added, have rows only from the passes after it. Rows nest, so a part's time
also counts in `_spawn_order`, and `placeable` time also counts in `focus_budget.place`.

The report prints no sum per row, so estimate each row's total as count times median, all from the same window;
percentiles do not add, and the estimate runs low for a row with a long tail, so check its p95 too.

| What the report shows | Where the time goes |
|---|---|
| The `launchable`, `ranks` or `ordered` estimate close to the `_spawn_order` estimate | That part is the slow one: the engine and capacity decision filters, the focus record selection over the ledger hierarchy, or the final focus sort |
| The `focus_budget.place` estimate close to the `_spawn_order` estimate | Placement is the slow part; compare it with the `placeable` estimate. When `placeable` is small, the time is in focus placement itself |
| `_running` above a few milliseconds | The lane holds many agent records or task rows; the filter makes no store or network call, so it grows only with those |
| The `ready_in_order` or `claim_view` estimate close to the `_spawn_order` estimate | Their swarm store reads, such as claims, launch failures and agent records, or their readiness checks are the slow part |

`tests/swarm/test_spawn_timing.py` holds the contract. A spawn pass records a row for each part before the spawn step
row ends. Spawn order records one `_running` and one `focus_budget.place` row per lane, each starting and ending
inside the `_spawn_order` row, and inside it one `launchable` row per lane and one `ranks` and `ordered` row per spawn
order. The running filter keeps only the lane's agents that have not ended and still own their
task, with a placeholder row for an agent whose task row is missing.

### Focus ranks in spawn order

`scripts.swarm.focus_budget.ranks(doc)` gives every task in the ledger document its focus rank, the position of the
first focus record that covers it; `focus_budget.ordered` then sorts the spawn queue focused tasks first, by rank band,
then by that rank. Only `focus` records count: freeze records in the same `freezes` collection take no position.

| Focus record target | Covers a task when | Example |
|---|---|---|
| A node address: `plans/<id>`, `phases/<id>`, `slices/<id>`, `tasks/<id>` | The address is in the task's ancestry: the task itself, its slice, phase and plan, and the chain of a task it was merged into | `phases/p1` covers every task of phase `p1` |
| A selector with a value: `lane:<lane>` or `kind:<kind>` | The task's lane or kind equals the value; everything after the first colon is the value | `kind:x:y` covers a task whose kind is `x:y` |
| A selector with no value, such as `lane:` | Never: it is read as a node address, and no node has that address | |

A task's rank is the lowest position among the records that cover it, so a task inside a focused phase that also
matches an earlier `kind:` focus takes the earlier position. A target focused twice keeps its first position. A task
no record covers takes the count of focus records, `focus_budget.outside(doc)`, which places it after every focused
task.

With these records, in order, on a ledger whose phase `p1` holds task `inside` and whose task `hot` has kind `hot`:

```text
freeze  phases/p1     no position
focus   kind:hot      position 0   covers hot
focus   phases/p1     position 1   covers inside
focus   lane:ci       position 2   covers every ci lane task
```

`hot` ranks 0, `inside` ranks 1, a ci lane task ranks 2 and every other task ranks 3. A task in `p1` with kind `hot`
ranks 0. Write the records with `agentihooks swarm <swarm slug> focus TARGET`; see
[Freeze and focus](../pillars/swarm.md#freeze-and-focus).

`ranks` reads the focus records once per call: node addresses go into one lookup from address to first position and
selectors into one ordered list. Each task then costs one lookup per node of its ancestry, at most the hierarchy depth
plus the merged chain, and one comparison per selector record, instead of a match of every focus record against every
task. On 3 November 2026, with the 284 focus records `rig-grade-swarm` held, one call over 3000 tasks went from
0.622 s to 0.006 s with the same ranks
([pull request 9573](https://github.com/the-cloud-clockwork/agentihooks/pull/9573)). Spawn order calls `ranks` once
per lane inside `focus_budget.place`, which orders the lane's ready tasks with it, and once more after the lanes to sort
the whole queue. Only that last call has its own `scripts.swarm.focus_budget.ranks` row; the per lane calls count in
each lane's `focus_budget.place` row.

Measure the spawn step over a thirty minute UTC window from its `swarm_tick_step` finished records:

```bash
SLUG=rig-grade-swarm
TZ=UTC journalctl --user -u agentihooks-swarm.service --since "2026-11-03 17:30:00" --until "2026-11-03 18:00:00" \
  | grep '"event": "swarm_tick_step"' | grep '"phase": "finished"' \
  | grep "\"slug\": \"$SLUG\"" | grep '"step": "spawn"' | sed 's/^[^{]*//' \
  | python3 -c 'import sys, json
w = sorted(json.loads(l)["wall_s"] for l in sys.stdin)
print(len(w), "spawn passes; median", round(w[len(w) // 2], 2) if w else None, "s; max", round(w[-1], 2) if w else None, "s")'
```

Measured this way on the workstation tick of `rig-grade-swarm`, the spawn step read 8 passes with a median of 27.37 s
and a max of 49.88 s from 17:30 to 18:00 UTC on 3 November 2026, and 9 passes with a median of 2.20 s and a max of
32.89 s from 22:56 to 23:17 UTC after the merge, as recorded in the proof of the ledger task the pull request
closed. The max stayed above the 5 s target, so the remaining slow passes are
in other parts of spawn order or in the launch itself; split them with the rows above and with
[Timed rows inside a fenced spawn](#timed-rows-inside-a-fenced-spawn).

`tests/swarm/test_focus_budget.py` holds the contract: ranks by node address and selector, first position wins,
duplicate targets, an empty selector value, colons inside task ids and selector values, and a test that ranks twenty
node address focuses without calling `freeze.covers` once.

| Symptom | Check |
|---|---|
| A task inside a focused phase launches after tasks of a later focus | An earlier `lane:` or `kind:` record covers the later tasks, or the task's `phase`, `slice` or `merged_into` link does not reach the focused node; read the task record and the ancestry its links give |
| A focus record never ranks any task | Its target is a selector with no value, such as `lane:`, or names a node that no task's ancestry holds; clear it with `unfreeze TARGET` |
| The `focus_budget.ranks` row, times one more than the lane count, is close to the `_spawn_order` row | Ranking is the slow part, since every lane's `focus_budget.place` row holds one more `ranks` call; the ledger document holds many tasks or selector records, and each selector record costs one comparison per task |

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
| `FencedRuntime.live_names` and the agent changes | Read and drop the pass's shared live names listing, described under [One live names listing per tick pass](#one-live-names-listing-per-tick-pass); they do not touch the lease. |
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
| `source_control` | `change`, `checks`, `runs`, `queue_health`, `tags`, `capabilities`, `issue_state`, `rate_limit`, `issues_enabled`, `latest_runs`, `merged_at`, `open_changes`, `workflow_runs`, `download`, `graph` | `enqueue`, `merge`, `comment`, `close_issue`, `cancel_run` |
| `inbox` | `read` | `send`, `close` |
| `llm` | none | `classify` |
| `metrics` | none | `emit` |
| `host` | `read` | `run` |
| `runtime` | `inspect`, `panes` | `launch`, `stop` |
| `processes` | `scan` | none |

A restricted port raises `TypeError("tools require a Deadline keyword argument")` when `deadline=` is missing or not a
`Deadline`, `TimeoutError("tool deadline expired")` when it has passed, and `AttributeError` for an operation outside
its port.

### Effects in a tick pass

`scripts.swarm.cli.run_tick` builds the real `Tools` once per pass and holds it in
`scripts.tick.ports.tools.PASS_TOOLS` until the pass ends. A `Context` built without `tools` inside the pass takes
that value; outside a pass it is `None`. The adapters are `ClientLedger` with service credentials, `StoreCoordination`
acting as the lease owner, `GitHubSourceControl` on the working directory, `RouterRuntime` over `routed(os.environ)`,
a fresh `PassProcesses`, `StoreInbox` sending as `tick@<slug>`, `ApiLlm` on the first classifier model, `SpoolMetrics`
over the local spool opened on its first append, and `local_host`.

`scripts.tick.engine.run_step` hands each step `Context(view, deadline, tools.for_effects(step.effects))`. A step
declares its effects and its feature map id on the `Step` protocol (`effects`, `feature`); `CallStep` and `WriteStep`
take them as keywords and default to `frozenset()` and `""`. A step that calls a write operation on a port it did not
declare raises `UndeclaredEffect`, and the engine records the step failed with
`scripts.tick.ports.contracts.UndeclaredEffect` as its error. The writes the step would have returned in its `Result`
are not applied; calls it already made through declared ports stay made.

Every registered step declares `effects=frozenset()` today and still reaches outside systems through the tick module.
A step moves onto ports in its own sweep, naming the ports it writes in `effects` in the same pull request.

#### Declaring effects on a step

`effects` holds port names from the table under [Declared effects](#declared-effects): `ledger`, `coordination`,
`source_control`, `runtime`, `processes`, `inbox`, `llm`, `metrics` and `host`. Read operations need no declaration;
declare a port only when the step calls one of its write operations. `feature` is the id of the step's entry in
`feature-map.json`.

The ports reach a step only through `ctx.tools` in its `run(ctx)`. A step class that implements the `Step` protocol
carries `effects` and `feature` as attributes and calls the ports with the deadline of its context:

```python
from dataclasses import dataclass

from scripts.tick import registry
from scripts.tick.step import Context, Domain, Lane, Result
from scripts.tick.steps.tasks import TASK_PASSES


@dataclass(frozen=True)
class WakeStep:
    """Send one wake to the first engineer seat of the example swarm `rig-grade-swarm` through the inbox port."""

    name: str = "example wake"
    group: str = TASK_PASSES
    lane: Lane = Lane("tick")
    budget_s: float = 5.0
    interval_s: float = 60.0
    needs: frozenset[str] = frozenset()
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    effects: frozenset[str] = frozenset({"inbox"})
    feature: str = "swarm-example-wake"

    def run(self, ctx: Context) -> Result:
        """Send the wake and return no writes."""
        ctx.tools.inbox.send("eng-1@rig-grade-swarm", "wake", deadline=ctx.deadline)
        return Result()


registry.register(WakeStep())
```

`CallStep` and `WriteStep` take the same two declarations as keywords after the positional name, group and call and
their domain arguments:

```python
from scripts.tick.steps import CallStep
from scripts.tick.steps.tasks import WriteStep, held

CallStep("example", TASK_PASSES, held(_example), effects=frozenset({"inbox"}), feature="swarm-example")
WriteStep("example sizes", TASK_PASSES, _sizes, effects=frozenset({"metrics"}), feature="swarm-example-sizes")
```

Their call receives the tick module and a pass state reader, never the `Context`, so a call step cannot reach
`ctx.tools`; until it moves into a step class its declared effects only record intent. A step that leaves out both
declarations has `effects=frozenset()` and `feature=""`: it may read through every port and write through none. A
name in `effects` that is not a port name opens nothing: `for_effects` checks each port by its own name, so a
misspelt `"source-control"` leaves `source_control` closed. Registration does not check effect names; a misspelt name
surfaces as `UndeclaredEffect` at the step's first write through that port.

#### Testing a step's effects

`fake_tools()` builds unrestricted fakes, and `engine.run_step` narrows them by the step's `effects` exactly as a pass
does. A test asserts the record's outcome and the calls each fake saw:

```python
from scripts.tick import engine
from scripts.tick.adapters import fakes
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context

tools = fakes.fake_tools()
step = WakeStep()
record = engine.run_step(step, Context(view, Deadline.after(60.0), tools))
assert (record.outcome, record.error) == (engine.OK, "")
assert [call.operation for call in tools.inbox.calls] == ["send"]
```

For a step that writes outside its effects the record reads `engine.FAILED` with
`"scripts.tick.ports.contracts.UndeclaredEffect"` as its error, and the fake of the undeclared port has no call. A test
that builds a `Context` without `tools` and needs the pass value sets `PASS_TOOLS` and resets it with the token:

```python
from scripts.tick.ports.tools import PASS_TOOLS

token = PASS_TOOLS.set(fakes.fake_tools())
try:
    assert Context(view, Deadline.after(1.0)).tools is PASS_TOOLS.get()
finally:
    PASS_TOOLS.reset(token)
```

The feature map entry `swarm-tick-declared-effects` links this behavior to `tests/tick/test_effects.py` and
`tests/swarm/test_tick_groups.py::test_the_wrapper_builds_the_real_tools_once_per_pass_for_every_group`; the
delivered change is [pull request 9632](https://github.com/the-cloud-clockwork/agentihooks/pull/9632).

#### Seeing a refused write

The refusal is raised before the deadline check and before the adapter is called, so the outside system is untouched.
Inside a swarm tick pass run by `run_tick` against the swarm's Redis store, the step's timing wrapper writes a finished
`swarm_tick_step` journal line with `"outcome": "error"` and `"error": "scripts.tick.ports.contracts.UndeclaredEffect"`,
stores the same fields in `tick:steps`, adds the step to the pass's failed steps, and `tick:records` stores the step
with outcome `failed` (see [Tick report](tick-report.md)). A slow lane step that ends after its group returned also
prints `slow lane step <name> failed: scripts.tick.ports.contracts.UndeclaredEffect`. Find refusals on the workstation
tick with:

```bash
TZ=UTC journalctl --user -u agentihooks-swarm.service --since "1 hour ago" --until now | grep UndeclaredEffect
```

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
| `issue_state`, `close_issue`, `rate_limit`, `issues_enabled`, `latest_runs`, `cancel_run`, `merged_at`, `open_changes`, `workflow_runs`, `download`, `graph` | `None` | `None` |

An entry in `answers` always wins, with or without a queue. A step that must work on a provider without a merge
queue is tested against the default fake; give every operation that falls back to `None` an answer whenever the step
reads it.

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
| `test_every_adapter_method_checks_its_deadline` fails with `unchecked: <module>.<method>` | That adapter method takes a `deadline` keyword but its first statement does not call `deadline.live()`. | Make `deadline.live()` the first statement after the docstring and local imports, as under [Writing an adapter method with a deadline](#writing-an-adapter-method-with-a-deadline). |
| `test_the_records_adapter_takes_no_deadline` fails | A function or method in `records_redis.py` gained a keyword-only `deadline` argument. | Remove the argument: the test expects the module to take no deadline, and its writes rely on the fence's lease and revision checks. |
| A fleet red listing ends before 60 seconds | `fleet_red.read` was given a caller deadline with less time left, and `child` kept the earlier expiry. | Give the calling step a larger budget; the listing never runs past `SECONDS`. |
| `UndeclaredEffect: <port> is not a declared effect` | A step called a write operation on a port missing from its effects. | Declare the port in the step's effects. |
| A step fails with `UndeclaredEffect` although its effects name the port | The name in `effects` is misspelt, such as `"source-control"` for `source_control`, so no port opened. | Use the port names from the table under [Declared effects](#declared-effects). |
| `AttributeError: 'NoneType' object has no attribute '<port>'` inside a step | `ctx.tools` is `None`: the `Context` was built outside a tick pass, for example by calling `scripts.swarm.tick.tick` or `run_group` directly instead of through `run_tick`. | Run the pass through `scripts.swarm.cli.run_tick`, or in a test pass `tools` to `Context` or set `PASS_TOOLS`. |
| `test_only_the_reap_and_spawn_steps_declare_their_effects_yet` fails naming a step | A registered step other than reap and spawn now declares effects, or reap or spawn changed its ports. | Update that test in the same pull request that moves the step onto ports. |
| `TypeError: tools require a Deadline keyword argument` | A restricted port was called without `deadline=`. | Pass the step's `Deadline` by keyword. |
| `KeyError: 'unknown metrics table: <name>'` | `emit` named a table the adapter was not built with. | Add the `Table` to the `tables` mapping. |
| `BackendFailure: <model>: timeout` from `classify` | The decisions API did not answer within `deadline.remaining`. | Raise the deadline, or build `ApiLlm` with a model that answers faster. |
| `InboxError: done needs an outcome naming where the work went` from `inbox.close` | The item was not sent with `fyi`, and the step closed it as `done` with no detail. | Pass the detail naming where the work went, or close with `handoff`, `blocked` or `cancel`. |
| `Outcome(..., Status.UNAVAILABLE, "", detail="no agent named <handle>")` from `runtime.inspect` or `runtime.stop` | The `agents` lookup found no record for that name. | Check the name against the swarm's agent records; the router was not called. |
| `TypeError` from `runtime.launch` | The request mapping lacks one of `config`, `lane`, `name`, `task`, or carries another key. | Pass exactly those four keys. |
| `runtime.panes` returns `[]` | herdr answered without a `panes` list. | Run `herdr pane list` on the host and check the herdr server is up. |
| The `spawn` step row is slow and its `FencedRuntime._refusal` row carries most of it | A placement read runs outside a placement pass, so each launch scans `/proc` and reads the host again, or a new read is not routed through `PassCache.once`. | Route a repeated read through `PassCache.once`, as under [One placement scan per placement pass](#one-placement-scan-per-placement-pass). |
| The `spawn` step row is slow and its `FencedRuntime.runtime_spawn` row carries most of it | The wrapped runtime's launch is slow: a herdr pane, a Kubernetes worker or a hive launch. | Read that runtime's own rows in the same tick report; the pass cache does not cover it. |
| Two launches in one pass are refused as local full although a session ended during the pass | The pass reads the live session scan once, so the ended session still counts until the pass ends. | No action; the next pass scans again. |
| A reaped or retired process was not found in a tick pass | The process exited before selection, so its `/proc` entry was gone and the snapshot was kept. | No action; the process is already gone. |
| A step in a tick pass sees an agent as live after it was stopped, or misses one that started | The change went through the wrapped runtime, such as `original_runtime` or `herdr_runtime`, or through a forwarded method outside `AGENT_CHANGES`, so the pass listing was kept. | Make the change through the `FencedRuntime` with one of the listed operations, or add the operation to `AGENT_CHANGES`, as under [One live names listing per tick pass](#one-live-names-listing-per-tick-pass). |
| The live names listing runs on every read in a tick pass | The caller holds the raw runtime given to `run_tick`, not the `FencedRuntime`, or reads before `PASS_TOOLS` is set or after it is reset. | Read through the pass's `FencedRuntime`; the wrapper probes group runs before it exists and lists on each call. |
| `test_running_pass_lists_live_names_once_plus_once_per_launch` fails with `LIVE_NAMES_LISTINGS=<n> LAUNCHES=<m>` | The running scenario's listing count moved: a change dropped the listing too often, stopped dropping it, or changed the number of launches. | Compare the counts with the change; a new listing read after the last launch or a new agent change adds one listing. |
| HTTP 409 `stale_controller`, `the controller lease is stale`, on `ledger.mutate` or a `LedgerClient` write | The operation carried a `controller_epoch` that is no longer the live lease of that ledger: another controller took over. | No action in the step. The pass stops at its next step boundary, where `keep_tick` raises, and a later pass acquires the lease again once no other controller holds it. |
| `SwarmError: the controller lease is stale` at the start of a pass or before a launch | `held_pass` renewal or `require_epoch` found another controller's lease. | Check which hive holds the swarm's lease; a pass on this controller resumes only once it acquires the lease. |
| `coordination.lease` returns `None` | Another owner holds a live lease, or the swarm's controller host names another owner. | Run the step on the controller that holds the lease. |
| `coordination.lock` returns `None` | Another holder keeps the named lock until its expiry. | Skip the guarded work this pass; the lock expires with the holder's deadline. |
| `SwarmError` from `coordination.configure` | A changed field failed the store's checks, such as an unknown `state` or `autonomy`. | Pass a value the store accepts. |
| `ledger.export` or `ledger.mutate` runs past its deadline | The deadline bounds each HTTP request, and a call can make several. | Size the deadline for the retries, or check `deadline.remaining` between calls. |
| `Unsupported(name)` from `host.read` or `host.run` | The host has no reading or job by that name. | Add it to the `JobHost` mappings or handle the missing capability in the step. |
| Semgrep `swarm-source-control-port` finding in a file under `scripts/swarm` or `scripts/tick` | The file builds a `gh` command, as a list or through `github.command`. | Call the matching port operation instead; add one to the port as described under [The gate on gh commands](#the-gate-on-gh-commands) when none fits. |
| `source control exceptions grew: [<paths>]` from the Semgrep workflow | The pull request added a path to `.semgrep/source-control-allowlist.json`. | Remove it and move that file's calls onto the port. |
| `SwarmError: cannot tell whether the repo has issues: <detail>` from `swarm park` | The `issues_enabled` read failed, timed out, or GitHub answered without `has_issues`. | Check `gh api repos/<owner>/<name>` from the worktree, then park again. |
| `panic cancel failed: <error>` in the tick output | A fix pull request or a fleet repository's run list could not be read, or a cancel was refused. | Check the `--fix` address and the login's access to each fleet repository; the tick retries a minute later. |
| `test_no_tick_step_module_imports_an_adapted_implementation` fails | A tick module imports one of the adapted implementations. | Route the call through the matching adapter. |
