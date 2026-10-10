import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, prompt
from scripts.swarm import tick as tick_module
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import master_scale, masters
from scripts.swarm_v2.masters_channel import MastersChannel
from tests.swarm.test_tick import FakeRuntime

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "swarm_v2" / "master-scaling.json").read_text())
SLUG = FIXTURE["slug"]
PHASES = FIXTURE["phases"]
LEAD, SECOND, THIRD, FOURTH = masters.seats(SLUG, 4)
NOW = 1_800_000_000_000
MINUTE = 60_000
HOLD = master_scale.HOLD_MS
DOC = {"phases": [{"id": p, "title": f"phase {p}"} for p in PHASES], "tasks": [], "_meta": {"rev": 1, "events": []}}
LEAD_NAME = f"{SLUG}-master-1"


@pytest.fixture
def swarm():
    import fakeredis

    server = fakeredis.FakeServer()
    store = RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 20, 0))
    store.update(SLUG, state="running")
    master_scale.set_rule(store.redis, SLUG, master_scale.rule_of(FIXTURE["rule"]))
    runtime = FakeRuntime()
    runtime.server = server
    store.put_agent(SLUG, AgentRecord(LEAD_NAME, MASTER, MASTER, pane_id="pm1", seat=LEAD))
    store.seats.occupy(LEAD, LEAD_NAME, 1)
    runtime.live.add(LEAD_NAME)
    return store, runtime


def staffed(store, size, hives=("",)):
    for agent in store.agents(SLUG):
        if agent.lane != MASTER:
            store.drop_agent(SLUG, agent.name)
    for i in range(size):
        name = f"{SLUG}-eng-{i}"
        store.put_agent(SLUG, AgentRecord(name, "eng", f"t{i}", seat=f"eng-{i}@{SLUG}", hive=hives[i % len(hives)]))


def run(store, runtime, now=NOW):
    return master_scale.run(SLUG, store.config(SLUG), store, runtime, DOC, now)


def extras(store):
    found = [a for a in store.agents(SLUG) if a.lane == MASTER and a.seat != LEAD and a.state != "finished"]
    return sorted(found, key=lambda a: a.seat)


def owners(store):
    return masters.MasterSeats(store.redis).owners(SLUG, DOC)


def count(store):
    return masters.MasterSeats(store.redis).count(SLUG)


def posts(store):
    return MastersChannel(store.redis)._posts(SLUG, 0)


def hand_off(store, runtime, agent):
    store.put_handoff(SLUG, agent.task, f"# Handoff v2\nnotes of {agent.seat}", seat=agent.seat)
    store.put_agent(SLUG, replace(agent, state="finished"))
    runtime.live.discard(agent.name)


def grown(swarm):
    store, runtime = swarm
    staffed(store, 20)
    run(store, runtime)
    return extras(store)


def asked(swarm, start=NOW + MINUTE):
    store, runtime = swarm
    seated = grown(swarm)
    staffed(store, 5)
    assert run(store, runtime, start) == []
    run(store, runtime, start + HOLD)
    return seated


@pytest.mark.parametrize("step", FIXTURE["steps"])
def test_the_fixture_wants_one_master_seat_per_five_working_agents(step):
    working = [AgentRecord(f"e{i}", "eng", f"t{i}") for i in range(step["agents"])]
    ignored = [AgentRecord("done", "eng", "tx", state="finished"), AgentRecord("m", MASTER, MASTER)]
    assert master_scale.wanted(FIXTURE["rule"], working + ignored) == step["wanted"]


def test_one_master_seat_per_hive_with_a_floor_of_one():
    working = [AgentRecord(f"e{i}", "eng", f"t{i}", hive=hive) for i, hive in enumerate(["a", "a", "b", ""])]
    assert master_scale.wanted(master_scale.PER_HIVE, working) == 3
    assert master_scale.wanted(master_scale.PER_HIVE, []) == 1
    assert master_scale.wanted("5", []) == 1


def test_a_rule_reads_a_whole_number_hive_or_off():
    assert master_scale.rule_of("5") == "5"
    assert master_scale.rule_of("hive") == master_scale.PER_HIVE
    assert master_scale.rule_of("off") == ""


@pytest.mark.parametrize("text", ["0", "-1", "three", "", "5.5", "2=3"])
def test_any_other_rule_is_refused(text):
    with pytest.raises(masters.MasterError) as refused:
        master_scale.rule_of(text)
    assert str(refused.value) == f"master-per takes a whole number of agents per master, hive or off, not {text!r}"


def test_growing_from_five_to_twenty_agents_launches_a_master_in_each_new_seat_and_posts_the_owners(swarm):
    store, runtime = swarm
    staffed(store, 5)
    assert run(store, runtime) == []
    assert count(store) == 1 and runtime.masters == [] and posts(store) == []

    staffed(store, 20)
    actions = run(store, runtime, NOW + MINUTE)

    seated = extras(store)
    assert [(a.seat, a.task) for a in seated] == [(SECOND, "master-2"), (THIRD, "master-3"), (FOURTH, "master-4")]
    assert actions == [
        "master seats 1 to 4 for 20 working agents",
        *(f"spawned master {a.name} in seat {a.seat}" for a in seated),
    ]
    assert count(store) == 4
    found = owners(store)
    assert Counter(found.values()) == {LEAD: 4, SECOND: 4, THIRD: 4, FOURTH: 4}
    [post] = posts(store)
    assert (post.seat, post.by) == ("swarm", "swarm")
    assert post.text.startswith("Master seats are now 4 for 20 working agents.")
    for address in (LEAD, SECOND, THIRD, FOURTH):
        assert f"{address} owns {', '.join(p for p in PHASES if found[p] == address)}" in post.text

    name, task = runtime.masters[0]
    assert (task["id"], task["seat"]) == ("master-2", SECOND)
    assert task["phases"] == [p for p in PHASES if found[p] == SECOND]
    text = prompt.build(SLUG, "/repo", MASTER, name, task)
    assert f"You hold master seat {SECOND}, one of 4 masters of swarm {SLUG}." in text
    assert ", ".join(task["phases"]) in text
    assert f"agentihooks swarm {SLUG} masters-channel read" in text

    assert run(store, runtime, NOW + 2 * MINUTE) == []
    assert len(runtime.masters) == 3 and len(posts(store)) == 1


def test_the_lead_prompt_names_no_extra_seat():
    text = prompt.build(SLUG, "/repo", MASTER, LEAD_NAME, {"id": MASTER, "seat": LEAD, "phases": PHASES})
    assert "You hold master seat" not in text


def test_a_live_extra_master_never_blocks_the_lead_seat_launch(swarm):
    store, runtime = swarm
    store.drop_agent(SLUG, LEAD_NAME)
    runtime.live.discard(LEAD_NAME)
    store.put_agent(SLUG, AgentRecord(f"{SLUG}-master-2", MASTER, "master-2", seat=SECOND))
    runtime.live.add(f"{SLUG}-master-2")

    actions = tick_module._master(SLUG, store.config(SLUG), store, runtime, NOW)

    [lead] = [a for a in store.agents(SLUG) if a.seat == LEAD]
    assert actions == [f"spawned master {lead.name}"]
    assert runtime.masters[-1][1]["id"] == MASTER


def test_without_a_rule_the_count_stays_manual_and_its_empty_seats_are_launched(swarm):
    store, runtime = swarm
    master_scale.set_rule(store.redis, SLUG, master_scale.rule_of("off"))
    staffed(store, 20)
    assert run(store, runtime) == []
    assert count(store) == 1 and runtime.masters == []

    masters.MasterSeats(store.redis).set_count(SLUG, 2)
    actions = run(store, runtime, NOW + MINUTE)
    [second] = extras(store)
    assert actions == [f"spawned master {second.name} in seat {SECOND}"]
    assert count(store) == 2 and posts(store) == []


def test_swarm_set_stores_a_rule_and_refuses_an_unknown_one(swarm, monkeypatch, capsys):
    store, _ = swarm
    store.create(SwarmConfig("paused-sw", "/repo", 1, 1, state="paused"))
    monkeypatch.setattr(cli, "connect", lambda: store)
    assert cli.main(["paused-sw", "set", "master-per=hive"]) == 0
    assert master_scale.rule(store.redis, "paused-sw") == master_scale.PER_HIVE
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["master_per"] == "hive"
    assert cli.main(["paused-sw", "set", "master-per=lots"]) == 1
    assert cli.main(["paused-sw", "set", "master-per=4", "bogus=1"]) != 0
    assert master_scale.rule(store.redis, "paused-sw") == master_scale.PER_HIVE
    assert cli.main(["paused-sw", "set", "master-per=off"]) == 0
    assert master_scale.rule(store.redis, "paused-sw") == ""


def test_a_shrink_inside_the_hold_window_asks_nobody_and_a_regrowth_restarts_the_window(swarm):
    store, runtime = swarm
    seated = grown(swarm)
    staffed(store, 5)
    assert run(store, runtime, NOW + MINUTE) == []
    assert run(store, runtime, NOW + MINUTE + HOLD - 1) == []
    staffed(store, 20)
    assert run(store, runtime, NOW + MINUTE + HOLD) == []
    staffed(store, 5)
    assert run(store, runtime, NOW + 2 * MINUTE + HOLD) == []
    assert run(store, runtime, NOW + 2 * MINUTE + 2 * HOLD - 1) == []
    assert count(store) == 4
    assert all(InboxStore(store.redis).pending_items(a.seat) == [] for a in seated)


def test_a_retire_ask_goes_once_and_a_live_extra_master_keeps_its_seat(swarm):
    store, runtime = swarm
    seated = grown(swarm)
    staffed(store, 5)
    run(store, runtime, NOW + MINUTE)

    actions = run(store, runtime, NOW + MINUTE + HOLD)

    assert actions == [f"asked {a.name} to hand off retiring seat {a.seat}" for a in reversed(seated)]
    for agent in seated:
        [item] = InboxStore(store.redis).pending_items(agent.seat)
        assert item.sender == "swarm"
        assert f"seat {agent.seat} retires" in item.text and f"agentihooks swarm {SLUG} handoff" in item.text
    assert run(store, runtime, NOW + 2 * MINUTE + HOLD) == []
    assert count(store) == 4 and extras(store) == seated


def test_an_emptied_lower_seat_is_not_relaunched_while_a_higher_seat_retires(swarm):
    store, runtime = swarm
    second, _, _ = asked(swarm)
    hand_off(store, runtime, second)

    actions = run(store, runtime, NOW + 2 * MINUTE + HOLD)

    assert actions == []
    assert count(store) == 4 and len(runtime.masters) == 3


def test_twenty_back_to_five_retires_extra_seats_through_their_handoffs(swarm):
    store, runtime = swarm
    inbox = InboxStore(store.redis)
    second, third, fourth = asked(swarm)
    before = owners(store)
    question = inbox.send(f"{SLUG}-eng-1", FOURTH, "a question about the last phase")
    hand_off(store, runtime, third)
    hand_off(store, runtime, fourth)

    actions = run(store, runtime, NOW + 2 * MINUTE + HOLD)

    assert actions == [
        f"retired master seat {FOURTH} after 60 seconds",
        f"retired master seat {THIRD} after 60 seconds",
        "master seats 4 to 2 for 5 working agents",
    ]
    assert count(store) == 2 and len(runtime.masters) == 3
    after = owners(store)
    assert Counter(after.values()) == {LEAD: 8, SECOND: 8}
    assert all(after[p] == before[p] for p in PHASES if before[p] in (LEAD, SECOND))
    texts = [item.text for item in inbox.pending_items(LEAD)]
    assert any("notes of " + THIRD in text for text in texts)
    assert any("notes of " + FOURTH in text for text in texts)
    assert inbox.get(question.id).address == LEAD
    assert store.handoff(SLUG, "master-3") == store.handoff(SLUG, "master-4") == ""
    assert posts(store)[-1].text.startswith("Master seats are now 2 for 5 working agents.")

    hand_off(store, runtime, second)
    actions = run(store, runtime, NOW + 3 * MINUTE + HOLD)

    assert actions == [f"retired master seat {SECOND} after 120 seconds", "master seats 2 to 1 for 5 working agents"]
    assert count(store) == 1 and set(owners(store).values()) == {LEAD}
    assert len(posts(store)) == 3 and len(runtime.masters) == 3
    assert any("notes of " + SECOND in item.text for item in inbox.pending_items(LEAD))
    assert all(inbox.open_items(address) == [] for address in (SECOND, THIRD, FOURTH))
    assert master_scale.measurements(store.redis, SLUG) == {
        "master_seat_count": 1,
        "master_retire_seconds": [60, 60, 120],
    }

    import fakeredis

    restarted = RedisStore(fakeredis.FakeRedis(server=runtime.server, decode_responses=True))
    assert masters.MasterSeats(restarted.redis).owners(SLUG, DOC) == owners(store)
    assert master_scale.run(SLUG, restarted.config(SLUG), restarted, runtime, DOC, NOW + 4 * MINUTE + HOLD) == []


def test_a_retired_seat_moves_items_its_master_read_but_never_closed(swarm):
    store, runtime = swarm
    inbox = InboxStore(store.redis)
    _, _, fourth = asked(swarm)
    taken = inbox.send(f"{SLUG}-eng-1", FOURTH, "read but not answered")
    inbox.deliver(taken.id, FOURTH)
    [ask] = [i for i in inbox.inbox(FOURTH) if i.sender == "swarm"]
    inbox.deliver(ask.id, FOURTH)
    hand_off(store, runtime, fourth)

    run(store, runtime, NOW + 2 * MINUTE + HOLD)

    assert inbox.get(taken.id).address == LEAD and inbox.get(taken.id).state == "pending"
    assert inbox.get(ask.id).state == "cancelled"


def test_regrowth_after_the_asks_withdraws_them_and_keeps_every_seat(swarm):
    store, runtime = swarm
    inbox = InboxStore(store.redis)
    seated = asked(swarm)
    asks = [item for agent in seated for item in inbox.pending_items(agent.seat)]
    assert len(asks) == 3

    staffed(store, 20)
    assert run(store, runtime, NOW + 2 * MINUTE + HOLD) == []

    assert {inbox.get(item.id).state for item in asks} == {"cancelled"}
    assert count(store) == 4 and extras(store) == seated
    staffed(store, 5)
    assert run(store, runtime, NOW + 3 * MINUTE + HOLD) == []


def test_the_fixture_swarm_grows_and_shrinks_through_every_step(swarm):
    store, runtime = swarm
    now = NOW
    for step in FIXTURE["steps"]:
        staffed(store, step["agents"])
        run(store, runtime, now)
        run(store, runtime, now + HOLD)
        for agent in extras(store):
            if InboxStore(store.redis).pending_items(agent.seat):
                hand_off(store, runtime, agent)
        run(store, runtime, now + HOLD + MINUTE)
        assert count(store) == step["wanted"], step
        assert len(extras(store)) == step["wanted"] - 1
        assert set(owners(store).values()) == set(masters.seats(SLUG, step["wanted"]))
        now += HOLD + 2 * MINUTE


def test_an_extra_master_never_stands_in_for_a_lead_that_is_down(swarm):
    from scripts.swarm import control_notifications, tick_master

    store, runtime = swarm
    store.drop_agent(SLUG, LEAD_NAME)
    runtime.live.discard(LEAD_NAME)
    second = AgentRecord(f"{SLUG}-master-2", MASTER, "master-2", seat=SECOND)
    store.put_agent(SLUG, second)
    runtime.live.add(second.name)

    assert tick_master._bound(store, SLUG, runtime) is None
    assert control_notifications.master(store, SLUG) is None
    assert tick_module._recover_master(SLUG, store.config(SLUG), store, runtime, NOW) == []
    assert [a.name for a in store.agents(SLUG) if masters.is_lead(SLUG, a)] == []
