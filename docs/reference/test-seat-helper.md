---
title: Admission seat helper for tests
parent: Reference
nav_order: 94
---

# Admission seat helper for tests

Use `tests.helpers.seat.seat_agent` when a swarm test needs a task owner before
exercising another feature. It seats an agent through hub admission without
starting the swarm, running a tick pass or spawning a session. The shared
`seat` fixture binds the helper to the store and ledger supplied by `env`.

The waits and pull request confirmation fixtures use this setup. Their tests
can exercise wait declarations, wait resolution and branch confirmation
without relying on the old launch engine to produce an agent.

## Use the fixture

Import both `seat` and `env` into the test module. The shared `env` fixture
supplies a fake Redis store, ledger and runtime, with task `t1` in the `eng`
lane and task `t2` in the `ci` lane. Create the swarm before seating a task:

```python
from typing import Any

from scripts.swarm.store import RedisStore
from tests.helpers.seat import seat
from tests.swarm.test_cli import env, run

_fixtures = (env, seat)


def test_task_owner(env: tuple[RedisStore, Any, Any], seat: Any) -> None:
    store, ledger, runtime = env
    assert run("sw", "create", "--repo", "/repo") == 0

    agent = seat("t1")

    assert store.claimant("sw", "t1") == agent.name
    assert ledger.rows["t1"]["claimed_by"] == agent.name
    assert agent.state == "working"
    assert store.config("sw").state == "paused"
    assert runtime.spawned == []
```

`seat` always uses slug `sw`. Supply the helper directly when a fixture needs
another slug or is imported by other test modules that do not import `seat`.

## Seat agents in a shared fixture

The waits fixture and pull request confirmation setup create the swarm, then
call `seat_agent` for both tasks. A reusable fixture follows the same pattern:

```python
from typing import Any

import pytest

from scripts.swarm.store import RedisStore
from tests.helpers.seat import SLUG, seat_agent
from tests.swarm.test_cli import env, run

_fixture = env


@pytest.fixture
def started(env: tuple[RedisStore, Any, Any]) -> tuple[RedisStore, Any]:
    store, ledger, _ = env
    assert run(SLUG, "create", "--repo", "/repo") == 0
    seat_agent(store, ledger, SLUG, "t1")
    seat_agent(store, ledger, SLUG, "t2")
    return store, ledger
```

Keep the feature's own setup after these calls: for example, its fake clock,
pull request response or task branch. Preserve its assertions when replacing
launch setup. A tick call that tests wait resolution still belongs in the
test body; the helper replaces a tick used only to obtain an agent.

## Options and launch defaults

The direct call accepts `store`, `ledger`, `slug` and `task_id`, followed by
these optional arguments. The bound fixture forwards the same options.

| Argument | Default | Effect |
| --- | --- | --- |
| `hive` | `root` | Records which hive admits the task |
| `launch` | `None` | Overrides only the supplied launch decision fields |
| `clock` | `lease.wall_ms` | Supplies the admission clock used for naming and `started_at` |

The launch decision defaults are `harness="claude"`, `account="acct"`,
`model="opus"` and `effort="high"`. The profile is `engineer` for `eng`,
`cicd` for `ci`, and `engineer` for other lanes. For example, after creating
the swarm in the fixture example:

```python
agent = seat(
    "t1",
    hive="hive-a",
    launch={"harness": "codex", "model": "gpt-5"},
    clock=lambda: 1_000,
)
assert (agent.hive, agent.harness, agent.model) == ("hive-a", "codex", "gpt-5")
assert (agent.profile, agent.account, agent.effort) == ("engineer", "acct", "high")
assert agent.started_at == 1_000
```

Use this call in place of the first `seat("t1")`, rather than seating the
already claimed task a second time. These values describe a synthetic launch
decision; the helper does not select or authenticate a real account. No hub,
NATS connection or running hive is needed with the shared fake `env` fixture.

## State returned by the helper

The helper calls `Admission.admit` with the task's lane and claim generation,
then `Admission.launched` with the launch decision. On current dev it also
passes an `execution.started` event to the hub claim lives reactor. This last
step supplies the working state that feature tests need.

| State | Result |
| --- | --- |
| Task ownership | The generated agent name owns the claim; the ledger task is `claimed` by that name |
| Execution | The store allocates an execution identity and occupies a lane seat on the requested hive |
| Agent | `state="working"`, `pane_id="w1:p1"`, `launched_at=1000` |
| Claim life | The pending life is cleared and one started claim is counted |
| Runtime | No session is spawned or listed by the fake runtime |
| Launch row | No entry is written to the swarm's `launches` hash |
| Swarm | Its configuration stays as created, including `state="paused"` |

The pane and launch timestamp are test constants. Setting `clock` changes
admission time, not the fixed `launched_at`. A working record here is not
evidence that a real process or pane exists.

## Limits and migration scope

The helper uses a test readiness snapshot instead of the production
`readiness.Source`. It bypasses the running swarm state and the release,
apply and fleet red readiness waits. Use it to establish ownership for another
feature, not to prove those readiness gates or hive session startup.

A custom ledger must support `task`, `state` and guarded `update_task` calls
with `if_state` and `if_match`. Admission still checks the task and its claim;
an unavailable task is not made claimable by the fixture.

[Pull request 11167](https://github.com/the-cloud-clockwork/agentihooks/pull/11167)
migrated only the waits and pull request confirmation setup, with no assertion
changes or test deletions. Outage seat release was explicitly excluded from
that delivery. Recovery, live runtime membership, worktree and seat release
tests need their own feature setup beyond the state supplied by this helper.

## Verification and troubleshooting

`tests/helpers/test_seat.py` covers ownership, execution state, launch defaults,
selective overrides, custom hive and refused admission under feature
`test-seat-through-admission`. Read those tests alongside the migrated
fixtures when changing their setup. Tests run in CI; see the
[local test guard](local-test-guard.md) and [delivery tiers](delivery-tiers.md).

| Symptom | Check |
| --- | --- |
| Fixture `seat` or `env` is missing | Import both into a module using the bound fixture; direct helper callers need their own `env` import |
| `hub admission refused <task>: <result>` | Create the swarm first, then check the task exists, is open and has no conflicting claim |
| `the launched acknowledgement for <task> was refused` | Check that the admitted task, hive and generation still match when the acknowledgement is written |
| The runtime does not list the working agent | Expected for this helper; provide runtime membership explicitly when that is part of the feature under test |
| Changing `clock` does not change `launched_at` | Admission time and the synthetic execution start timestamp are separate |

