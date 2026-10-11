import json
from types import SimpleNamespace

import pytest

from scripts.doctor import loop, priming
from scripts.swarm import ci_speed, freeze, time_left
from scripts.swarm.ledger_client import LedgerClient

pytestmark = pytest.mark.xdist_group("fakeredis")

OBSERVED = SimpleNamespace(quota_capacity=lambda *args: {})
PLAIN = SimpleNamespace(template="")
DOCTOR = SimpleNamespace(template=priming.TEMPLATE)


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

    def time_left(self, slug, slots, ci_minutes, **focus):
        self.sent.append((slug, slots, ci_minutes, *focus.items()))


@pytest.fixture
def store():
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    return SimpleNamespace(redis=redis, key=lambda slug, name: f"test:{slug}:{name}")


def observe(store):
    store.redis.set(ci_speed.key("sw"), json.dumps({"minutes": 4, "runs": 13}))
    store.redis.set(
        store.key("sw", "quota-capacity"),
        json.dumps(decision({"eng": 1, "ci": 0, "plan": 0}, {"eng": 1, "ci": 0, "plan": 0}, {"claude": 2, "codex": 0})),
    )


def test_refresh_sends_the_slots_and_the_cached_ci_median(store):
    observe(store)
    ledger = Ledger()
    assert time_left.refresh("sw", store, ledger, OBSERVED, PLAIN, {}) == []
    assert ledger.sent == [("sw", 2, 4)]


def test_a_runtime_without_a_quota_reader_sends_unobserved_slots(store):
    observe(store)
    ledger = Ledger()
    time_left.refresh("sw", store, ledger, object(), PLAIN, {})
    assert ledger.sent == [("sw", None, 4)]


def test_refresh_without_a_ci_median_or_capacity_sends_none(store):
    ledger = Ledger()
    time_left.refresh("sw", store, ledger, OBSERVED, PLAIN, {})
    assert ledger.sent == [("sw", None, None)]


def test_every_tick_sends_even_an_unchanged_estimate(store):
    observe(store)
    ledger = Ledger()
    for _ in range(3):
        assert time_left.refresh("sw", store, ledger, OBSERVED, PLAIN, {}) == []
    assert ledger.sent == [("sw", 2, 4)] * 3


def test_the_tick_focus_rides_with_the_inputs_only_when_set(store):
    observe(store)
    ledger = Ledger()
    time_left.refresh("sw", store, ledger, OBSERVED, DOCTOR, {freeze.WATCHED: ["the focus on x"]})
    time_left.refresh("sw", store, ledger, OBSERVED, PLAIN, {})
    assert ledger.sent == [("sw", 2, 4, ("fix_phase", loop.FIX_PHASE), ("watched", ["the focus on x"])), ("sw", 2, 4)]


def test_the_focus_reads_the_fix_phase_from_the_template_and_the_watched_focus_from_the_doc():
    assert time_left.focus(PLAIN, {}) == {}
    assert time_left.focus(PLAIN, {freeze.WATCHED: ["a"]}) == {"watched": ["a"]}
    assert time_left.focus(DOCTOR, {"tasks": []}) == {"fix_phase": loop.FIX_PHASE}


def test_only_as_many_watched_focus_names_as_the_op_schema_takes_are_sent():
    names = ["x" * 2500] + [f"focus {n}" for n in range(250)]
    assert time_left.focus(PLAIN, {freeze.WATCHED: names})["watched"] == ["x" * 2000] + [
        f"focus {n}" for n in range(99)
    ]
    assert (time_left.WATCHED_KEPT, time_left.WATCHED_LENGTH) == (100, 2000)


def test_a_ledger_without_the_write_is_skipped(store):
    assert time_left.refresh("sw", store, object(), OBSERVED, PLAIN, {}) == []


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


def test_the_client_carries_the_tick_focus(monkeypatch):
    calls = []
    monkeypatch.setattr(LedgerClient, "_call", lambda self, slug, ops=None: calls.append((slug, ops)))
    LedgerClient().time_left("sw", 3, None, fix_phase="p9", watched=["the focus on x"])
    [(_, [op])] = calls
    assert (op["fix_phase"], op["watched"]) == ("p9", ["the focus on x"])
