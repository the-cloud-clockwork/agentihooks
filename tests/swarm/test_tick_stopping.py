from scripts.swarm.store import MASTER, AgentRecord
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, store, tasks  # noqa: F401

ENGINEER = "engineer@a1b2c3-0001"


def _awaiting(name, lane, task):
    return AgentRecord(name, lane, task, state="awaiting-decision", started_at=1)


def test_stopping_with_an_empty_master_seat_ends_an_awaiting_decision_seat_and_stops(store):  # noqa: F811
    ledger = tasks(("t1", "eng"))
    store.claim("sw", "t1", ENGINEER, 60_000)
    store.put_agent("sw", _awaiting(ENGINEER, "eng", "t1"))
    store.update("sw", state="stopping")
    tick("sw", store, ledger, FakeRuntime(), now_ms=1_000)
    assert store.config("sw").state == "stopped"
    assert store.agents("sw") == [] and store.claimant("sw", "t1") is None


def test_stopping_keeps_an_awaiting_decision_seat_while_a_master_can_decide(store):  # noqa: F811
    store.put_agent("sw", _awaiting(ENGINEER, "eng", "t1"))
    store.put_agent("sw", _awaiting("master@a1b2c3-0002", MASTER, MASTER))
    store.update("sw", state="stopping")
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    assert store.config("sw").state == "stopping"
    assert {a.name for a in store.agents("sw")} == {ENGINEER, "master@a1b2c3-0002"}
