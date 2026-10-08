import json
from types import SimpleNamespace

import pytest

from scripts.swarm import ci_speed, time_left
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm_ledger import ledger_stats

pytestmark = pytest.mark.xdist_group("fakeredis")

NOW = 30 * 3_600_000
OBSERVED = SimpleNamespace(quota_capacity=lambda *args: {})


def decision(effective, allocation, placeable, configured=None):
    return {
        "configured": configured or {"eng": 6, "ci": 2, "plan": 1},
        "effective": effective,
        "allocation": {lane: {"claude": n, "codex": 0} for lane, n in allocation.items()},
        "placeable": placeable,
    }


def test_slots_count_running_agents_plus_the_seats_the_quota_can_place():
    found = decision({"eng": 4, "ci": 1, "plan": 0}, {"eng": 2, "ci": 0, "plan": 0}, {"claude": 3, "codex": 1})
    assert time_left.slots(found) == 7


def test_slots_never_pass_the_configured_lane_limits():
    found = decision({"eng": 6, "ci": 2, "plan": 1}, {"eng": 0, "ci": 0, "plan": 0}, {"claude": 9, "codex": 4})
    assert time_left.slots(found) == 9


def test_spent_quota_leaves_only_the_running_agents():
    found = decision({"eng": 2, "ci": 1, "plan": 0}, {"eng": 0, "ci": 0, "plan": 0}, {"claude": 0, "codex": 0})
    assert time_left.slots(found) == 3


def test_no_capacity_decision_is_unobserved():
    assert time_left.slots({}) is None


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


def ledger_doc(saved=None):
    meta = {"events": []}
    if saved is not None:
        meta["time_left"] = saved
    return {"tasks": [{"id": "a", "state": "open", "difficulty": "S"}], "_meta": meta}


def observe(store):
    store.redis.set(ci_speed.key("sw"), json.dumps({"minutes": 4, "runs": 13}))
    store.redis.set(
        store.key("sw", "quota-capacity"),
        json.dumps(decision({"eng": 1, "ci": 0, "plan": 0}, {"eng": 1, "ci": 0, "plan": 0}, {"claude": 2, "codex": 0})),
    )


def test_refresh_sends_the_slots_and_the_cached_ci_median(store):
    observe(store)
    ledger = Ledger()
    assert time_left.refresh("sw", store, ledger, OBSERVED, ledger_doc(), NOW) == []
    assert ledger.sent == [("sw", 2, 4)]


def test_a_runtime_without_a_quota_reader_sends_unobserved_slots(store):
    observe(store)
    ledger = Ledger()
    time_left.refresh("sw", store, ledger, object(), ledger_doc(), NOW)
    assert ledger.sent == [("sw", None, 4)]


def test_refresh_without_a_ci_median_or_capacity_sends_none(store):
    ledger = Ledger()
    time_left.refresh("sw", store, ledger, OBSERVED, ledger_doc(), NOW)
    assert ledger.sent == [("sw", None, None)]


def saved(minutes, inputs=None, tiers=None):
    calculation = {"minutes": minutes, "tiers": tiers or ledger_stats.agent_minutes(ledger_doc(), [], NOW)}
    return {"inputs": inputs or {"slots": 2, "ci_minutes": 4}, "calculation": calculation}


def test_an_estimate_that_moved_less_than_five_minutes_is_not_sent_again(store):
    observe(store)
    ledger = Ledger()
    time_left.refresh("sw", store, ledger, OBSERVED, ledger_doc(saved(14 - 4)), NOW)
    time_left.refresh("sw", store, ledger, OBSERVED, ledger_doc(saved(14 + 4)), NOW)
    assert ledger.sent == []
    time_left.refresh("sw", store, ledger, OBSERVED, ledger_doc(saved(14 + 5)), NOW)
    time_left.refresh("sw", store, ledger, OBSERVED, ledger_doc(saved(14 - 5)), NOW)
    assert ledger.sent == [("sw", 2, 4), ("sw", 2, 4)]


def test_changed_inputs_tiers_or_a_known_estimate_are_sent(store):
    observe(store)
    ledger = Ledger()
    time_left.refresh("sw", store, ledger, OBSERVED, ledger_doc(saved(14, inputs={"slots": 3, "ci_minutes": 4})), NOW)
    tiers = {"S": {"minutes": 9, "samples": 5}}
    time_left.refresh("sw", store, ledger, OBSERVED, ledger_doc(saved(14, tiers=tiers)), NOW)
    time_left.refresh("sw", store, ledger, OBSERVED, ledger_doc(saved(None)), NOW)
    assert ledger.sent == [("sw", 2, 4)] * 3


def test_moved_compares_unknown_estimates_by_presence():
    tiers = {"S": {"minutes": 10, "samples": 0}}
    inputs = {"slots": None, "ci_minutes": None}
    unknown = {"inputs": inputs, "calculation": {"minutes": None, "tiers": tiers}}
    assert time_left.moved(unknown, inputs, {"minutes": None, "tiers": tiers}) is False
    assert time_left.moved(unknown, inputs, {"minutes": 3, "tiers": tiers}) is True
    assert time_left.moved({}, inputs, {"minutes": None, "tiers": tiers}) is True
    assert time_left.MOVE_MINUTES == 5


def test_the_estimate_counts_the_ledger_events_against_now(store):
    observe(store)
    ledger = Ledger()
    doc = {
        "tasks": [{"id": "a", "state": "claimed", "difficulty": "S"}],
        "_meta": {"events": [{"kind": "task claimed", "target": "tasks/a", "at": NOW - 10 * 60_000}]},
    }
    doc["_meta"]["time_left"] = saved(4)
    time_left.refresh("sw", store, ledger, OBSERVED, doc, NOW)
    assert ledger.sent == []


def test_a_state_without_meta_still_sends(store):
    observe(store)
    ledger = Ledger()
    time_left.refresh("sw", store, ledger, OBSERVED, {"tasks": []}, NOW)
    assert ledger.sent == [("sw", 2, 4)]


def test_a_ledger_without_the_write_is_skipped(store):
    assert time_left.refresh("sw", store, object(), OBSERVED, ledger_doc(), NOW) == []


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
