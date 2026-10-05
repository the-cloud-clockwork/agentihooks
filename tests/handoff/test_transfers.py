from dataclasses import replace

import pytest

from scripts.handoff import transfers
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
DOC = "# Handoff v2\n## Next\nCheck the saved proof and verify it passed.\n## Read first\nNone\n"


@pytest.fixture
def setup():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    old = AgentRecord("sw-eng-1", "eng", "t1", seat="eng-1@sw", state="finished")
    store.put_agent("sw", old)
    store.seats.occupy(old.seat, old.name, 1)
    return store, old


def test_tick_binds_a_successor_without_claiming_continuity(setup):
    store, old = setup
    transfer = transfers.record(store, "sw", old, "recycle", DOC, 2)
    store.put_handoff("sw", "t1", DOC, seat=old.seat)
    runtime = FakeRuntime()
    tick("sw", store, FakeLedger([{"id": "t1"}]), runtime, 10)
    result = transfers.get(store, "sw", transfer["id"])
    assert result["successor"] == runtime.spawned[0][1]
    assert result["binding"]["state"] == "live"
    assert result["continuity"]["state"] == "pending"
    assert runtime.tasks[0]["transfer"]["id"] == transfer["id"]


def test_successor_confirms_reading_and_the_first_next_action(setup):
    store, old = setup
    transfer = transfers.record(store, "sw", old, "recycle", DOC, 2)
    new = replace(old, name="sw-eng-2", state="working", pane_id="pane")
    store.put_agent("sw", new)
    store.seats.occupy(new.seat, new.name, 3)
    transfers.attach(store, "sw", new, 3)
    result = transfers.confirm(store, "sw", transfer["id"], new, "Check the saved proof and verify it passed.", 4)
    assert result["continuity"] == {
        "state": "confirmed",
        "at": 4,
        "by": "sw-eng-2",
        "next": "Check the saved proof and verify it passed.",
    }
    assert result["binding"]["state"] == "pending"
    assert transfers.get(store, "sw", transfer["id"]) == result


def test_a_different_next_action_or_an_old_occupant_cannot_confirm(setup):
    store, old = setup
    transfer = transfers.record(store, "sw", old, "recycle", DOC, 2)
    new = replace(old, name="sw-eng-2", state="working")
    store.put_agent("sw", new)
    store.seats.occupy(new.seat, new.name, 3)
    transfers.attach(store, "sw", new, 3)
    with pytest.raises(SwarmError, match="first Next action"):
        transfers.confirm(store, "sw", transfer["id"], new, "Build something else", 4)
    store.seats.occupy(new.seat, "sw-eng-3", 5)
    with pytest.raises(SwarmError, match="occupant"):
        transfers.confirm(store, "sw", transfer["id"], new, transfer["next"], 6)


def test_a_missing_handoff_is_an_explicit_continuity_gap(setup):
    store, old = setup
    transfer = transfers.record(store, "sw", old, "restore", "", 2)
    assert transfer["continuity"]["state"] == "unknown"
    assert transfer["continuity"]["gap"] == "No handoff document"
