import json
from types import SimpleNamespace

import pytest

from scripts.swarm import ci_speed, time_left
from scripts.swarm.ledger_client import LedgerClient

pytestmark = pytest.mark.xdist_group("fakeredis")

CONFIG = SimpleNamespace(max_eng=6, max_ci=2, max_plan=1)


def decision(effective, allocation, placeable, configured=None):
    return {
        "configured": configured or {"eng": 6, "ci": 2, "plan": 1},
        "effective": effective,
        "allocation": {lane: {"claude": n, "codex": 0} for lane, n in allocation.items()},
        "placeable": placeable,
    }


def test_slots_count_running_agents_plus_the_seats_the_quota_can_place():
    found = decision({"eng": 4, "ci": 1, "plan": 0}, {"eng": 2, "ci": 0, "plan": 0}, {"claude": 3, "codex": 1})
    assert time_left.slots(CONFIG, found) == 7


def test_slots_never_pass_the_configured_lane_limits():
    found = decision({"eng": 6, "ci": 2, "plan": 1}, {"eng": 0, "ci": 0, "plan": 0}, {"claude": 9, "codex": 4})
    assert time_left.slots(CONFIG, found) == 9


def test_spent_quota_leaves_only_the_running_agents():
    found = decision({"eng": 2, "ci": 1, "plan": 0}, {"eng": 0, "ci": 0, "plan": 0}, {"claude": 0, "codex": 0})
    assert time_left.slots(CONFIG, found) == 3


def test_no_capacity_decision_falls_back_to_the_configured_limits():
    assert time_left.slots(CONFIG, {}) == 9


class Ledger:
    def __init__(self):
        self.sent = []

    def time_left(self, slug, slots, ci_minutes):
        self.sent.append((slug, slots, ci_minutes))


@pytest.fixture
def store():
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    return SimpleNamespace(redis=redis, key=lambda slug, name: f"test:{slug}:{name}")


def test_refresh_sends_the_slots_and_the_cached_ci_median(store):
    store.redis.set(ci_speed.key("sw"), json.dumps({"minutes": 4.43, "runs": 13}))
    store.redis.set(
        store.key("sw", "quota-capacity"),
        json.dumps(decision({"eng": 1, "ci": 0, "plan": 0}, {"eng": 1, "ci": 0, "plan": 0}, {"claude": 2, "codex": 0})),
    )
    ledger = Ledger()
    assert time_left.refresh("sw", CONFIG, store, ledger) == []
    assert ledger.sent == [("sw", 2, 4.43)]


def test_refresh_without_a_ci_median_or_capacity_sends_none_and_the_limits(store):
    ledger = Ledger()
    assert time_left.refresh("sw", CONFIG, store, ledger) == []
    assert ledger.sent == [("sw", 9, None)]


def test_a_ledger_without_the_write_is_skipped(store):
    assert time_left.refresh("sw", CONFIG, store, object()) == []


def test_the_client_sends_one_swarm_time_left_op(monkeypatch):
    calls = []
    monkeypatch.setattr(LedgerClient, "_call", lambda self, slug, ops=None: calls.append((slug, ops)))
    LedgerClient().time_left("sw", 3, 5.5)
    [(slug, [op])] = calls
    assert slug == "sw"
    assert op["id"].startswith("time_left-")
    assert {k: v for k, v in op.items() if k != "id"} == {
        "op": "time_left",
        "by": "swarm",
        "slots": 3,
        "ci_minutes": 5.5,
    }
