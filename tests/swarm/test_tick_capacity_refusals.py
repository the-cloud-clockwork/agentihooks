import json

import pytest

from scripts.swarm import capacity
from scripts.swarm import tick as reconcile
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store


@pytest.mark.parametrize("observed_at", [1_000, 999, None])
@pytest.mark.parametrize(("planned", "harness"), [({"t1": "codex"}, "claude"), ({"t1": "claude"}, "codex")])
def test_reservation_refusal_retains_only_this_pass_capacity(store, observed_at, planned, harness):
    error = reconcile.SpawnError("account reservation is full", "unavailable")
    error.harness = harness
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(crash=error)
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


@pytest.mark.parametrize(("reserved", "week_left"), [("interactive master", 90), ("", 10)])
def test_open_accounts_excluded_by_handoff_do_not_establish_free_capacity(store, reserved, week_left):
    error = reconcile.SpawnError("account reservation is full", "unavailable")
    error.harness = "claude"
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(crash=error)
    row = capacity.record(capacity.Account("claude", "a", "OPEN", 0, 90, week_left, 6, reserved=reserved))
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps({"observed_at": 1_000, "accounts": [row]}))
    actions = reconcile.tick("sw", store, ledger, runtime, now_ms=1_000)
    context = next(action for action in actions if action.startswith("spawn capacity for t1: "))
    assert json.loads(context.split(": ", 1)[1]) == {"harness": "claude", "accounts": []}


@pytest.mark.parametrize("error_type", [reconcile.SpawnError, OSError])
def test_missing_capacity_and_refusal_harness_stay_unknown(store, error_type):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(crash=error_type("account reservation is full"))
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps({"observed_at": 1_000}))
    actions = reconcile.tick("sw", store, ledger, runtime, now_ms=1_000)
    context = next(action for action in actions if action.startswith("spawn capacity for t1: "))
    assert json.loads(context.split(": ", 1)[1]) == {"harness": "", "accounts": []}
