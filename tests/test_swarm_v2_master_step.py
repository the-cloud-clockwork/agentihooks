import json

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_server
from scripts.swarm_v2 import master_scale, masters
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.unit

SLUG = "step-sw"
LEAD, SECOND, THIRD = masters.seats(SLUG, 3)
NOW = 1_800_000_000_000
MINUTE = 60_000
COOL_OFF = 10 * MINUTE
DOC = {"phases": [{"id": "p1", "title": "phase p1"}], "tasks": [], "_meta": {"rev": 1, "events": []}}
LEAD_NAME = f"{SLUG}-master-1"


@pytest.fixture
def swarm():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 30, 30, max_plan=30, masters_step=12))
    store.update(SLUG, state="running")
    runtime = FakeRuntime()
    store.put_agent(SLUG, AgentRecord(LEAD_NAME, MASTER, MASTER, pane_id="pm1", seat=LEAD))
    store.seats.occupy(LEAD, LEAD_NAME, 1)
    runtime.live.add(LEAD_NAME)
    return store, runtime


def staffed(store, size, lanes=("eng",)):
    for agent in store.agents(SLUG):
        if agent.lane != MASTER:
            store.drop_agent(SLUG, agent.name)
    for i in range(size):
        lane = lanes[i % len(lanes)]
        store.put_agent(SLUG, AgentRecord(f"{SLUG}-{lane}-{i}", lane, f"t{i}", seat=f"{lane}-{i}@{SLUG}"))


def run(store, runtime, now=NOW):
    return master_scale.run(SLUG, store.config(SLUG), store, runtime, DOC, now)


def count(store):
    return masters.MasterSeats(store.redis).count(SLUG)


def seated(store):
    return sorted(a.seat for a in store.agents(SLUG) if a.lane == MASTER and a.state != "finished")


def asks(store, address):
    return [i for i in InboxStore(store.redis).pending_items(address) if i.sender == master_scale.SENDER]


def hand_off(store, runtime, address):
    [agent] = [a for a in store.agents(SLUG) if a.seat == address]
    store.put_handoff(SLUG, agent.task, f"# Handoff v2\nnotes of {address}", seat=address)
    store.put_agent(SLUG, AgentRecord(agent.name, MASTER, agent.task, seat=address, state="finished"))
    runtime.live.discard(agent.name)


def at_three(swarm):
    store, runtime = swarm
    staffed(store, 24)
    run(store, runtime)
    assert count(store) == 3
    return store, runtime


def test_swarm_create_stores_a_step_of_twelve(swarm, monkeypatch):
    store, _ = swarm
    monkeypatch.setattr(cli, "connect", lambda: store)
    ledger = FakeLedger([])
    ledger.say = lambda slug, text, by=None: None
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)

    assert cli.main(["new-step-sw", "create", "--repo", "/repo"]) == 0

    assert store.config("new-step-sw").masters_step == 12


def test_a_swarm_stored_before_the_step_loads_zero(swarm):
    store, _ = swarm
    store.redis.hdel(store.key(SLUG, "config"), "masters_step")
    assert store.config(SLUG).masters_step == 0


@pytest.mark.parametrize(("agents", "wanted"), [(0, 1), (11, 1), (12, 2), (23, 2), (24, 3), (36, 4)])
def test_wanted_masters_are_one_plus_running_agents_over_the_step(agents, wanted):
    found = [AgentRecord(f"e{i}", "eng", f"t{i}") for i in range(agents)]
    ignored = [AgentRecord("m2", MASTER, "master-2"), AgentRecord("done", "eng", "tx", state="finished")]
    assert master_scale.stepped(12, found + ignored) == wanted


def test_a_swarm_under_one_step_keeps_its_single_master(swarm):
    store, runtime = swarm
    staffed(store, 11, lanes=("eng", "ci", "plan"))
    assert run(store, runtime) == []
    assert count(store) == 1 and runtime.masters == []


def test_twelve_engineer_ci_and_planner_agents_launch_master_two(swarm):
    store, runtime = swarm
    staffed(store, 12, lanes=("eng", "ci", "plan"))

    actions = run(store, runtime)

    assert actions[0] == "master seats 1 to 2 for 12 working agents"
    assert actions[1].endswith(f"in seat {SECOND}")
    assert count(store) == 2 and seated(store) == sorted([LEAD, SECOND])
    assert [task["id"] for _, task in runtime.masters] == ["master-2"]


def test_twenty_four_agents_launch_master_three(swarm):
    store, runtime = swarm
    staffed(store, 12)
    run(store, runtime)
    staffed(store, 24)

    actions = run(store, runtime, NOW + MINUTE)

    assert actions[0] == "master seats 2 to 3 for 24 working agents"
    assert actions[1].endswith(f"in seat {THIRD}")
    assert [task["id"] for _, task in runtime.masters] == ["master-2", "master-3"]


def test_no_downscale_inside_the_cool_off(swarm):
    store, runtime = at_three(swarm)
    staffed(store, 0)

    assert run(store, runtime, NOW + MINUTE) == []
    assert run(store, runtime, NOW + MINUTE + COOL_OFF - 1) == []

    assert count(store) == 3 and asks(store, SECOND) == asks(store, THIRD) == []
    assert run(store, runtime, NOW + MINUTE + COOL_OFF) != []
    assert len(asks(store, THIRD)) == 1


def test_a_rise_while_still_below_resets_the_cool_off(swarm):
    store, runtime = at_three(swarm)
    staffed(store, 0)
    assert run(store, runtime, NOW + MINUTE) == []
    staffed(store, 12)
    assert run(store, runtime, NOW + 5 * MINUTE) == []

    assert run(store, runtime, NOW + MINUTE + COOL_OFF) == []
    assert asks(store, THIRD) == [] and asks(store, SECOND) == []

    run(store, runtime, NOW + 5 * MINUTE + COOL_OFF)
    assert len(asks(store, THIRD)) == 1 and asks(store, SECOND) == []


def test_a_rise_withdraws_the_ask_of_a_seat_wanted_again(swarm):
    store, runtime = at_three(swarm)
    staffed(store, 0)
    run(store, runtime, NOW + MINUTE)
    run(store, runtime, NOW + MINUTE + COOL_OFF)
    [second_ask], [third_ask] = asks(store, SECOND), asks(store, THIRD)

    staffed(store, 12)
    run(store, runtime, NOW + 2 * MINUTE + COOL_OFF)

    inbox = InboxStore(store.redis)
    assert inbox.get(second_ask.id).state == "cancelled"
    assert inbox.get(third_ask.id).state == "pending"
    retiring = store.redis.hget(masters.MasterSeats(store.redis).key(SLUG), master_scale.RETIRING)
    assert set(json.loads(retiring)) == {THIRD}


def test_the_highest_backend_master_retires_and_returns_its_items_to_the_pool(swarm):
    store, runtime = at_three(swarm)
    inbox = InboxStore(store.redis)
    staffed(store, 12)
    run(store, runtime, NOW + MINUTE)
    run(store, runtime, NOW + MINUTE + COOL_OFF)
    assert asks(store, SECOND) == [] and len(asks(store, THIRD)) == 1
    question = inbox.send(f"{SLUG}-eng-1", THIRD, "a question for the third master")
    hand_off(store, runtime, THIRD)

    actions = run(store, runtime, NOW + 2 * MINUTE + COOL_OFF)

    assert actions[0].startswith(f"retired master seat {THIRD}")
    assert count(store) == 2 and seated(store) == sorted([LEAD, SECOND])
    assert inbox.get(question.id).address == LEAD and inbox.get(question.id).state == "pending"
    assert inbox.open_items(THIRD) == []


def test_the_front_door_master_never_retires_by_scaling(swarm):
    store, runtime = at_three(swarm)
    staffed(store, 0)
    run(store, runtime, NOW + MINUTE)
    run(store, runtime, NOW + MINUTE + COOL_OFF)
    hand_off(store, runtime, SECOND)
    hand_off(store, runtime, THIRD)

    run(store, runtime, NOW + 2 * MINUTE + COOL_OFF)

    assert count(store) == 1 and seated(store) == [LEAD]
    assert not any(f"so seat {LEAD} retires" in i.text for i in InboxStore(store.redis).pending_items(LEAD))


def test_a_zero_step_keeps_the_manual_count(swarm):
    store, runtime = swarm
    store.update(SLUG, masters_step=0)
    staffed(store, 24)
    assert run(store, runtime) == []
    assert count(store) == 1


def test_a_manual_master_count_turns_the_step_off_and_holds_under_twelve_agents(swarm, monkeypatch):
    store, runtime = swarm
    store.update(SLUG, state="paused")
    monkeypatch.setattr(cli, "connect", lambda: store)

    assert cli.main([SLUG, "set", "masters=3"]) == 0

    assert store.config(SLUG).masters_step == 0
    staffed(store, 5)
    run(store, runtime)
    assert run(store, runtime, NOW + COOL_OFF + MINUTE) == []
    assert count(store) == 3 and seated(store) == sorted([LEAD, SECOND, THIRD])


def test_an_explicit_master_per_rule_still_wins(swarm):
    store, runtime = swarm
    master_scale.set_rule(store.redis, SLUG, "5")
    staffed(store, 12)
    run(store, runtime)
    assert count(store) == 3


def test_swarm_set_stores_the_masters_step_beside_the_lane_caps(swarm, monkeypatch, capsys):
    store, _ = swarm
    store.create(SwarmConfig("paused-step", "/repo", 1, 1, state="paused"))
    monkeypatch.setattr(cli, "connect", lambda: store)

    assert cli.main(["paused-step", "set", "masters-step=6"]) == 0

    assert store.config("paused-step").masters_step == 6
    shown = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert (shown["max_eng"], shown["masters_step"]) == (1, 6)
    assert cli.main(["paused-step", "set", "masters-step=many"]) == 1
    assert store.config("paused-step").masters_step == 6


@pytest.mark.parametrize("step", [0, 12, 50])
def test_the_page_sets_the_masters_step(step):
    assert ledger_server.control_argv({"action": "set", "masters_step": step}) == ["set", f"masters-step={step}"]


@pytest.mark.parametrize("step", [-1, 51, True, "12"])
def test_the_page_refuses_an_invalid_masters_step(step):
    with pytest.raises(ValueError, match="masters_step"):
        ledger_server.control_argv({"action": "set", "masters_step": step})
