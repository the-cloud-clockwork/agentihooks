from dataclasses import replace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm.store import MASTER, AgentRecord
from scripts.swarm.tick import STARTUP_GRACE_MS, Placed, tick
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks  # noqa: F401


class RecoveryRuntime(FakeRuntime):
    def recover(self, name):
        return Placed("hand:p1", "codex")


@pytest.mark.parametrize("record", ["stale", "missing"])
def test_tick_recovers_live_master_without_spawning_or_moving_mail(store, record):  # noqa: F811
    store.update("sw", state="paused")
    dead = store.next_name("sw", MASTER)
    live = store.next_name("sw", MASTER)
    seat = "master@sw"
    if record == "stale":
        store.put_agent("sw", AgentRecord(dead, MASTER, MASTER, seat=seat))
    store.seats.occupy(seat, live, 1)
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", live, "Keep working")
    generation = store.seats.occupant(seat).generation
    runtime = RecoveryRuntime()
    runtime.live.add(live)

    actions = tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)

    assert runtime.masters == []
    assert [a.name for a in masters(store)] == [live]
    assert masters(store)[0].pane_id == "hand:p1"
    assert masters(store)[0].harness == "codex"
    assert store.seats.occupant(seat).generation == generation
    assert inbox.get(item.id).address == live
    assert f"adopted live master {live}" in actions
    tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 3)
    assert runtime.masters == []


def test_tick_recovers_a_master_whose_seat_was_restored_to_a_dead_occupant(store):  # noqa: F811
    dead = store.next_name("sw", MASTER)
    live = store.next_name("sw", MASTER)
    store.put_agent("sw", AgentRecord(dead, MASTER, MASTER, seat="master@sw"))
    store.seats.occupy("master@sw", dead, 1)
    runtime = RecoveryRuntime()
    runtime.live.add(live)

    tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)

    assert runtime.masters == []
    assert [a.name for a in masters(store)] == [live]
    assert store.seats.occupant("master@sw").occupant == live


@pytest.mark.parametrize("name", ["hand-opened-master", "sw-master-7"])
def test_recovery_uses_the_live_seat_occupant_without_a_registered_name(store, name):  # noqa: F811
    store.seats.occupy("master@sw", name, 1)
    runtime = RecoveryRuntime()
    runtime.live.add(name)
    tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)
    assert runtime.masters == []
    assert [a.name for a in masters(store)] == [name]


def test_stale_pane_on_a_dead_record_is_not_closed_during_recovery(store):  # noqa: F811
    dead = store.next_name("sw", MASTER)
    live = store.next_name("sw", MASTER)
    store.put_agent("sw", AgentRecord(dead, MASTER, MASTER, pane_id="hand:p1"))
    store.seats.occupy("master@sw", live, 1)
    runtime = RecoveryRuntime()
    runtime.live.add(live)
    tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)
    assert "hand:p1" not in runtime.closed


def test_a_stopped_swarm_recovers_its_live_master_before_reaping_on_wake(store):  # noqa: F811
    store.update("sw", state="stopped")
    dead = store.next_name("sw", MASTER)
    live = store.next_name("sw", MASTER)
    store.put_agent("sw", AgentRecord(dead, MASTER, MASTER, pane_id="hand:p1"))
    store.seats.occupy("master@sw", live, 1)
    InboxStore(store.redis).send("operator", "master@sw", "Resume the master")
    runtime = RecoveryRuntime()
    runtime.live.add(live)

    tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)

    assert "hand:p1" not in runtime.closed
    assert runtime.masters == []
    assert [a.name for a in masters(store)] == [live]
    assert store.config("sw").state == "paused"


def test_a_live_master_in_another_swarm_does_not_prevent_respawn(store):  # noqa: F811
    runtime = RecoveryRuntime()
    runtime.live.add("master@ffffff-0001")
    tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)
    assert len(runtime.masters) == 1


def test_runtime_recovers_the_live_herdr_pane_and_harness():
    from scripts.swarm.runtime import HerdrRuntime

    calls = []

    def herdr(args):
        calls.append(args)
        return {"agent": {"pane_id": "hand:p1", "agent": "codex"}}

    runtime = HerdrRuntime(herdr=herdr)
    assert runtime.recover("master@a1b2c3-0001") == Placed("hand:p1", "codex")
    assert calls == [["agent", "get", "master-a1b2c3-0001"]]


def test_runtime_keeps_a_live_master_when_herdr_cannot_locate_its_pane():
    from scripts.swarm.runtime import HerdrRuntime

    def unavailable(args):
        raise RuntimeError("herdr down")

    assert HerdrRuntime(herdr=unavailable).recover("hand-opened-master") == Placed("", "")


def test_a_finished_master_is_retired_instead_of_adopted(store):  # noqa: F811
    runtime = RecoveryRuntime()
    tick("sw", store, tasks(), runtime, 1)
    old = masters(store)[0]
    store.put_agent("sw", replace(old, state="finished"))
    tick("sw", store, tasks(), runtime, 2)
    assert old.name in runtime.killed
    assert len(runtime.masters) == 2
