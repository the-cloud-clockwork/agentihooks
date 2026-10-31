import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm.store import MASTER, AgentRecord
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, store, tasks  # noqa: F401

ENGINEER = "engineer@a1b2c3-0001"


def _awaiting(name, lane, task):
    return AgentRecord(name, lane, task, state="awaiting-decision", started_at=1)


@pytest.mark.parametrize(
    ("row", "after"),
    [
        ({"state": "claimed", "claimed_by": ENGINEER}, {"state": "open", "claimed_by": ""}),
        ({"state": "pr", "claimed_by": ENGINEER}, {"state": "open", "claimed_by": ""}),
        ({"state": "done", "claimed_by": ENGINEER}, {"state": "done", "claimed_by": ENGINEER}),
        ({"state": "claimed", "claimed_by": "other"}, {"state": "claimed", "claimed_by": "other"}),
    ],
)
def test_stopping_with_an_empty_master_seat_ends_an_awaiting_decision_seat_and_stops(store, row, after):  # noqa: F811
    ledger = tasks(("t1", "eng"))
    ledger.rows["t1"].update(row)
    store.claim("sw", "t1", ENGINEER, 60_000)
    store.put_agent("sw", _awaiting(ENGINEER, "eng", "t1"))
    item = InboxStore(store.redis).send("operator", ENGINEER, "are you there")
    store.update("sw", state="stopping")
    tick("sw", store, ledger, FakeRuntime(), now_ms=1_000)
    assert store.config("sw").state == "stopped"
    assert store.agents("sw") == [] and store.claimant("sw", "t1") is None
    assert {key: ledger.rows["t1"][key] for key in after} == after
    assert InboxStore(store.redis).get(item.id).state != "pending"
    assert InboxStore(store.redis).seats.exit_of(ENGINEER) == {"seat": "", "reason": "stopped", "generation": 0}


def test_stopping_keeps_an_awaiting_decision_seat_while_a_master_can_decide(store):  # noqa: F811
    store.put_agent("sw", _awaiting(ENGINEER, "eng", "t1"))
    store.put_agent("sw", _awaiting("master@a1b2c3-0002", MASTER, MASTER))
    store.update("sw", state="stopping")
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    assert store.config("sw").state == "stopping"
    assert {a.name for a in store.agents("sw")} == {ENGINEER, "master@a1b2c3-0002"}


def test_a_finished_master_left_after_a_refused_retire_does_not_keep_the_awaiting_seat(store):  # noqa: F811
    master = "master@a1b2c3-0002"
    runtime, ledger = FakeRuntime(), tasks(("t1", "eng"))
    runtime.stuck.add(master)
    store.put_agent("sw", _awaiting(ENGINEER, "eng", "gone"))
    store.put_agent("sw", AgentRecord(master, MASTER, MASTER, state="finished", started_at=1))
    store.update("sw", state="stopping")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert store.config("sw").state == "stopping"
    assert [a.name for a in store.agents("sw")] == [master]
    runtime.stuck.clear()
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert store.config("sw").state == "stopped"
