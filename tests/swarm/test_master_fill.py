from dataclasses import replace

import pytest

from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import MASTER_DOWN, STARTUP_GRACE_MS, tick
from scripts.swarm_v2 import master_fill
from scripts.swarm_v2.masters import MasterSeats
from tests.swarm.test_tick import FakeRuntime, masters, tasks

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return s


def test_a_raised_master_count_launches_a_master_in_every_seat(store):
    MasterSeats(store.redis).set_count("sw", 3)
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, 1)
    tick("sw", store, ledger, runtime, 2)
    assert sorted(a.seat for a in masters(store)) == ["master-2@sw", "master-3@sw", "master@sw"]
    assert len(runtime.masters) == 3


def test_a_master_that_ended_in_seat_two_is_respawned_in_seat_two(store):
    MasterSeats(store.redis).set_count("sw", 2)
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, 1)
    (second,) = [a for a in masters(store) if a.seat == "master-2@sw"]
    store.put_agent("sw", replace(second, state="finished"))
    runtime.live.discard(second.name)
    tick("sw", store, ledger, runtime, 2 + STARTUP_GRACE_MS)
    live = [a for a in masters(store) if a.state != "finished"]
    assert sorted(a.seat for a in live) == ["master-2@sw", "master@sw"]
    assert len(runtime.masters) == 3


def test_a_seat_two_master_that_crashed_without_finishing_is_dropped_and_refilled(store):
    from unittest.mock import patch

    from scripts.swarm import launch_check

    MasterSeats(store.redis).set_count("sw", 2)
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    with patch.object(launch_check, "begin") as begun:
        tick("sw", store, ledger, runtime, 1)
    (second,) = [a for a in masters(store) if a.seat == "master-2@sw"]
    assert second.state == "working"
    seat_checks = [c.args for c in begun.call_args_list if c.args[2].seat == second.seat]
    assert [(s is store, slug, record.name, at) for s, slug, record, at in seat_checks] == [
        (True, "sw", second.name, 1)
    ]
    runtime.live.discard(second.name)
    tick("sw", store, ledger, runtime, 2 + STARTUP_GRACE_MS)
    (refilled,) = [a for a in masters(store) if a.seat == "master-2@sw"]
    assert refilled.name != second.name and refilled.name in runtime.live


def test_a_live_seat_two_master_neither_blocks_nor_stands_in_for_an_ended_lead(store):
    MasterSeats(store.redis).set_count("sw", 2)
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, 1)
    (lead,) = [a for a in masters(store) if a.seat == "master@sw"]
    (second,) = [a for a in masters(store) if a.seat == "master-2@sw"]
    store.put_agent("sw", replace(lead, state="finished"))
    runtime.live.discard(lead.name)
    tick("sw", store, ledger, runtime, 2 + STARTUP_GRACE_MS)
    live = {a.seat: a.name for a in masters(store) if a.state != "finished"}
    assert live["master-2@sw"] == second.name
    assert live["master@sw"] not in {lead.name, second.name}
    assert len(runtime.masters) == 3


def test_a_seat_two_master_is_told_its_seat_count_and_owned_phases(store):
    MasterSeats(store.redis).set_count("sw", 2)
    runtime = FakeRuntime()
    tick("sw", store, tasks(("t1", "eng")), runtime, 1)
    (lead_task,) = [task for _, task in runtime.masters if task["id"] == MASTER]
    (seat_task,) = [task for _, task in runtime.masters if task["id"] == "master-2"]
    assert (seat_task["seat"], seat_task["masters"], seat_task["handoff"]) == ("master-2@sw", 2, "")
    assert isinstance(seat_task["owns"], list)
    assert "masters" not in lead_task


def test_a_seat_two_master_starts_from_its_seat_handoff_and_the_handoff_is_cleared(store):
    MasterSeats(store.redis).set_count("sw", 2)
    store.put_handoff("sw", "master-2", "seat two handoff")
    store.redis.hset(store.key("sw", "launch-assignments"), "master-2", '{"profile": "master"}')
    runtime = FakeRuntime()
    actions = tick("sw", store, tasks(("t1", "eng")), runtime, 1)
    (seat_task,) = [task for _, task in runtime.masters if task["id"] == "master-2"]
    assert (seat_task["handoff"], seat_task["launch_assignment"]) == ("seat two handoff", {"profile": "master"})
    assert store.handoff("sw", "master-2") == ""
    assert not store.redis.hexists(store.key("sw", "launch-assignments"), "master-2")
    assert "spawned master master@a1b2c3-0002 in seat master-2@sw" in actions


def test_no_seat_two_master_starts_without_a_session_slot(store):
    MasterSeats(store.redis).set_count("sw", 2)
    runtime = FakeRuntime(full=True)
    actions = tick("sw", store, tasks(("t1", "eng")), runtime, 1)
    assert not [a for a in masters(store) if a.seat == "master-2@sw"]
    assert "no session slot for the master, waiting" in actions


def test_a_failed_seat_two_launch_leaves_no_record_and_says_why(store):
    MasterSeats(store.redis).set_count("sw", 2)
    runtime = FakeRuntime()
    tick("sw", store, tasks(("t1", "eng")), runtime, 1)
    (second,) = [a for a in masters(store) if a.seat == "master-2@sw"]
    store.put_agent("sw", replace(second, state="finished"))
    runtime.live.discard(second.name)
    runtime.fail = True
    actions = tick("sw", store, tasks(("t1", "eng")), runtime, 2 + STARTUP_GRACE_MS)
    assert "master spawn failed in seat master-2@sw: herdr down" in actions
    assert not [a for a in masters(store) if a.seat == "master-2@sw" and a.state != "finished"]


def test_a_stopping_swarm_launches_no_seat_two_master(store):
    MasterSeats(store.redis).set_count("sw", 2)
    runtime = FakeRuntime()
    assert master_fill.run("sw", store.update("sw", state="stopping"), store, runtime, {}, 1) == []
    assert runtime.masters == []


def test_a_live_seat_two_master_keeps_a_down_lead_from_counting_as_an_outage(store):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import tick_master

    MasterSeats(store.redis).set_count("sw", 2)
    runtime, ledger = FakeRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    (lead,) = [a for a in masters(store) if a.seat == "master@sw"]
    runtime.live.discard(lead.name)
    runtime.full = True
    InboxStore(store.redis).send("operator", "master@sw", "anyone there")
    tick("sw", store, ledger, runtime, 1 + STARTUP_GRACE_MS + 5)
    assert MASTER_DOWN not in ledger.notes
    assert tick_master.read(store, "sw") == {}


class Recording(FakeRuntime):
    def __init__(self, refusal=""):
        super().__init__()
        self.refusal, self.asked, self.configs = refusal, [], []

    def placement_refusal(self, lane, task):
        self.asked.append((lane, task))
        return self.refusal

    def spawn(self, config, lane, name, task):
        self.configs.append(config)
        return super().spawn(config, lane, name, task)


@pytest.mark.parametrize(
    ("state", "live", "launched"), [("finished", False, 1), ("finished", True, 0), ("working", False, 0)]
)
def test_a_seat_is_free_only_once_its_master_finished_and_left(store, state, live, launched):
    MasterSeats(store.redis).set_count("sw", 2)
    store.put_agent("sw", AgentRecord("old", MASTER, "master-2", state=state, seat="master-2@sw"))
    runtime = Recording()
    if live:
        runtime.live.add("old")
    master_fill.run("sw", store.config("sw"), store, runtime, {}, 1)
    assert len(runtime.masters) == launched


def test_a_seat_launch_gets_the_swarm_config_and_the_phases_and_hives_its_seat_owns(store):
    seats = MasterSeats(store.redis)
    seats.set_count("sw", 2)
    seats.own_hive("sw", "anton-1", "master-2@sw")
    doc = {"phases": [{"id": "p1"}, {"id": "p2"}]}
    runtime, config = Recording(), store.config("sw")
    master_fill.run("sw", config, store, runtime, doc, 1)
    owned = sorted(item for item, owner in seats.owners("sw", doc).items() if owner == "master-2@sw")
    ((_, task),) = runtime.masters
    assert task["owns"] == owned and "hives/anton-1" in owned and {"p1", "p2"} & set(owned)
    assert runtime.configs == [config]


def test_a_placement_refusal_holds_the_seat_and_says_why(store):
    MasterSeats(store.redis).set_count("sw", 2)
    runtime = Recording("placed on another host")
    assert master_fill.run("sw", store.config("sw"), store, runtime, {}, 1) == ["placed on another host"]
    assert runtime.asked == [(MASTER, {"id": "master-2"})] and runtime.masters == []


def test_no_session_slot_holds_the_seat_and_says_why(store):
    MasterSeats(store.redis).set_count("sw", 2)
    runtime = FakeRuntime(full=True)
    assert master_fill.run("sw", store.config("sw"), store, runtime, {}, 1) == [
        "no session slot for the master, waiting"
    ]


def test_a_full_host_holds_the_seat_with_the_hold_it_names(store):
    from unittest.mock import patch

    from scripts.swarm import seat_spawn

    MasterSeats(store.redis).set_count("sw", 2)
    with patch.object(seat_spawn, "host_hold", return_value="holding the master spawn") as held:
        actions = master_fill.run("sw", store.config("sw"), store, FakeRuntime(), {}, 7)
    assert actions == ["holding the master spawn"]
    assert held.call_args.args == ("sw", store, 7, MASTER)
