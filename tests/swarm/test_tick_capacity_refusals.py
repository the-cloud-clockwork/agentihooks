import json

import pytest

from scripts.swarm import capacity
from scripts.swarm import tick as reconcile
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store


@pytest.mark.parametrize("observed_at", [1_000, 999, None])
@pytest.mark.parametrize(("planned", "harness"), [({}, "claude"), ({"t1": "codex"}, "codex")])
def test_reservation_refusal_retains_only_this_pass_capacity(store, observed_at, planned, harness):
    ledger, runtime = (
        tasks(("t1", "eng")),
        FakeRuntime(crash=reconcile.SpawnError("account reservation is full", "unavailable")),
    )
    decision = {
        "observed_at": observed_at,
        "tasks": planned,
        "accounts": [capacity.record(capacity.Account("claude", "a", "OPEN", 6, 90, 90, 6))],
    }
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps(decision))
    actions = reconcile.tick("sw", store, ledger, runtime, now_ms=1_000)
    context = next(action for action in actions if action.startswith("spawn capacity for t1: "))
    snapshot = json.loads(context.split(": ", 1)[1])
    assert snapshot == {
        "harness": harness,
        "accounts": decision["accounts"] if observed_at == 1_000 else [],
    }
    assert actions.index(context) + 1 == next(
        i for i, action in enumerate(actions) if action.startswith("spawn failed for t1")
    )
