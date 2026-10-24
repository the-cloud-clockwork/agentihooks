import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, metrics_outbox, prompt
from scripts.swarm import tick as tick_module
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import master_scale, masters
from scripts.swarm_v2.masters_channel import MastersChannel
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.unit

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
    assert task["owns"] == sorted(p for p in PHASES if found[p] == SECOND)
    text = prompt.build(SLUG, "/repo", MASTER, name, task)
    assert f"You hold master seat {SECOND}, one of 4 masters of swarm {SLUG}." in text
    assert ", ".join(task["owns"]) in text
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
    inbox = InboxStore(store.redis)
    second, _, _ = asked(swarm)
    question = inbox.send(f"{SLUG}-eng-1", SECOND, "a question for seat two")
    owners_before = owners(store)
    held = [p for p in PHASES if owners_before[p] == SECOND]
    hand_off(store, runtime, second)

    actions = run(store, runtime, NOW + 2 * MINUTE + HOLD)

    assert actions == [f"retired master seat {SECOND} after 60 seconds"]
    assert count(store) == 4 and len(runtime.masters) == 3
    assert any(f"notes of {SECOND}" in item.text for item in inbox.pending_items(LEAD))
    assert inbox.get(question.id).address == LEAD
    moved = owners(store)
    assert SECOND not in moved.values()
    assert held and all(moved[p] == LEAD for p in held)
    assert all(moved[p] == before for p, before in owners_before.items() if before != SECOND)
    assert masters.route(f"phases/{held[0]}", DOC, lambda: owners(store), [LEAD, THIRD, FOURTH], LEAD) == moved[held[0]]
    text = master_scale.EARLY.format(address=SECOND, moved=", ".join(f"{p} to {moved[p]}" for p in held))
    assert posts(store)[-1].text == text
    assert posts(store)[-1].at == (NOW + 2 * MINUTE + HOLD) / 1000 and len(posts(store)) == 2
    assert run(store, runtime, NOW + 3 * MINUTE + HOLD) == []
    assert SECOND not in owners(store).values()


def test_a_regrowth_after_an_early_retire_refills_the_seat_with_an_even_share(swarm):
    store, runtime = swarm
    second, _, _ = asked(swarm)
    hand_off(store, runtime, second)
    run(store, runtime, NOW + 2 * MINUTE + HOLD)

    staffed(store, 20)
    actions = run(store, runtime, NOW + 3 * MINUTE + HOLD)

    assert store.redis.hget(masters.MasterSeats(store.redis).key(SLUG), masters.RETIRED) is None
    assert any(action.endswith(f"in seat {SECOND}") for action in actions)
    assert Counter(owners(store).values())[SECOND] == len(PHASES) // 4


def test_a_lower_count_forgets_a_retired_seat_it_no_longer_holds(swarm):
    store, _ = swarm
    seats = masters.MasterSeats(store.redis)
    seats.set_count(SLUG, 4)
    for address in (SECOND, THIRD, FOURTH):
        seats.retire(SLUG, address)

    seats.set_count(SLUG, 3)

    assert seats.retired(SLUG) == [SECOND, THIRD]
    assert set(owners(store).values()) == {LEAD}


def test_a_partial_regrowth_refills_a_retired_seat_it_now_wants_with_an_even_share(swarm):
    store, runtime = swarm
    second, _, _ = asked(swarm)
    hand_off(store, runtime, second)
    run(store, runtime, NOW + 2 * MINUTE + HOLD)

    staffed(store, 10)
    actions = run(store, runtime, NOW + 3 * MINUTE + HOLD)

    assert masters.MasterSeats(store.redis).retired(SLUG) == []
    assert any(action.endswith(f"in seat {SECOND}") for action in actions)
    assert Counter(owners(store).values())[SECOND] == len(PHASES) // 4


def test_turning_the_rule_off_refills_a_retired_seat_with_its_phases(swarm):
    store, runtime = swarm
    seats = masters.MasterSeats(store.redis)
    seats.set_count(SLUG, 4)
    seats.retire(SLUG, SECOND)
    master_scale.set_rule(store.redis, SLUG, "")

    run(store, runtime)

    assert seats.retired(SLUG) == []
    assert SECOND in owners(store).values()


def test_an_early_retired_seat_hands_its_hive_back_to_the_lead(swarm):
    store, _ = swarm
    seats = masters.MasterSeats(store.redis)
    seats.set_count(SLUG, 4)
    seats.own_hive(SLUG, "h1", THIRD)

    master_scale._retire_early(SLUG, store, DOC, THIRD, [NOW, "none"], NOW + MINUTE)

    assert THIRD not in owners(store).values()
    assert posts(store)[-1].text.endswith(f"{masters.HIVES}/h1 to {LEAD}.")


def test_an_early_retired_seat_that_owns_no_phase_says_so(swarm):
    store, _ = swarm
    empty = {**DOC, "phases": []}

    action = master_scale._retire_early(SLUG, store, empty, THIRD, [NOW, "none"], NOW + MINUTE)

    assert action == f"retired master seat {THIRD} after 60 seconds"
    assert posts(store)[-1].text == f"Master seat {THIRD} retired owning no phase."
    assert masters.MasterSeats(store.redis).retired(SLUG) == [THIRD]


def test_twenty_back_to_five_retires_extra_seats_through_their_handoffs(swarm, tmp_path, monkeypatch):
    path = tmp_path / "metrics.sqlite"
    sink = metrics_outbox.Settings("http://metrics.invalid", "", "")
    monkeypatch.setattr(metrics_outbox, "settings", lambda environ: sink)
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: path)
    monkeypatch.setattr(metrics_outbox, "post", lambda sink, query, body: False)
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
    assert Counter(after.values()) == {LEAD: 12, SECOND: 4}
    assert all(after[p] == LEAD for p in PHASES if before[p] in (THIRD, FOURTH))
    assert all(after[p] == before[p] for p in PHASES if before[p] in (LEAD, SECOND))
    texts = [item.text for address in (LEAD, SECOND) for item in inbox.pending_items(address)]
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

    box = metrics_outbox.Outbox(path, sink)
    try:
        rows = box.recent("master_scaling", NOW + 4 * MINUTE + HOLD)
    finally:
        box.close()
    assert {(row["metric"], row["value"], row["seat"], row["ts_ms"]) for row in rows} == {
        ("master_seat_count", 4, "", NOW),
        ("master_retire_seconds", 60, FOURTH, NOW + 2 * MINUTE + HOLD),
        ("master_retire_seconds", 60, THIRD, NOW + 2 * MINUTE + HOLD),
        ("master_seat_count", 2, "", NOW + 2 * MINUTE + HOLD),
        ("master_retire_seconds", 120, SECOND, NOW + 3 * MINUTE + HOLD),
        ("master_seat_count", 1, "", NOW + 3 * MINUTE + HOLD),
    }
    assert all(row["ledger"] == SLUG for row in rows)
    assert all((row["plan"], row["phase"], row["slice"], row["task"]) == ("", "", "", "") for row in rows)
    assert len({row["event_id"] for row in rows}) == 6

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
    from scripts.swarm import control_notifications

    store, runtime = swarm
    store.drop_agent(SLUG, LEAD_NAME)
    runtime.live.discard(LEAD_NAME)
    second = AgentRecord(f"{SLUG}-master-2", MASTER, "master-2", seat=SECOND)
    store.put_agent(SLUG, second)
    runtime.live.add(second.name)

    assert control_notifications.master(store, SLUG) is None
    assert tick_module._recover_master(SLUG, store.config(SLUG), store, runtime, NOW) == []
    assert [a.name for a in store.agents(SLUG) if masters.is_lead(SLUG, a)] == []


def test_the_shrink_state_lives_in_this_swarms_masters_hash_and_the_ask_names_the_new_count(swarm):
    store, runtime = swarm
    grown(swarm)
    staffed(store, 5)
    key = masters.MasterSeats(store.redis).key(SLUG)
    run(store, runtime, NOW + MINUTE)
    assert store.redis.hget(key, master_scale.BELOW) == str(NOW + MINUTE)

    run(store, runtime, NOW + MINUTE + HOLD)

    [item] = InboxStore(store.redis).pending_items(FOURTH)
    assert item.text == master_scale.ASK.format(slug=SLUG, want=1, address=FOURTH, lead=LEAD)
    assert set(json.loads(store.redis.hget(key, master_scale.RETIRING))) == {SECOND, THIRD, FOURTH}


def test_a_retired_seat_forwards_its_handoff_and_moves_and_withdraws_items_as_the_swarm(swarm):
    store, runtime = swarm
    inbox = InboxStore(store.redis)
    _, _, fourth = asked(swarm)
    [ask] = inbox.pending_items(FOURTH)
    question = inbox.send(f"{SLUG}-eng-1", FOURTH, "a question")
    store.put_handoff(SLUG, fourth.task, "# Handoff v2\nnotes", seat=FOURTH)
    store.put_agent(SLUG, replace(fourth, state="finished"))
    runtime.live.discard(fourth.name)

    run(store, runtime, NOW + 2 * MINUTE + HOLD)

    forwarded = [i.text for i in inbox.pending_items(LEAD) if i.sender == "swarm"]
    assert forwarded == [master_scale.HANDED.format(address=FOURTH, slug=SLUG) + "# Handoff v2\nnotes"]
    assert inbox.history(ask.id)[-1]["by"] == "swarm"
    assert inbox.history(question.id)[-1]["by"] == "swarm"


def test_a_regrowth_withdraws_the_asks_as_the_swarm(swarm):
    store, runtime = swarm
    inbox = InboxStore(store.redis)
    seated = asked(swarm)
    asks = [item for agent in seated for item in inbox.pending_items(agent.seat)]
    staffed(store, 20)

    run(store, runtime, NOW + 2 * MINUTE + HOLD)

    assert [inbox.history(item.id)[-1]["by"] for item in asks] == ["swarm"] * 3


def test_retiring_a_seat_no_master_was_asked_to_leave_moves_its_items_to_the_lead(swarm):
    store, _ = swarm
    inbox = InboxStore(store.redis)
    waiting = inbox.send(f"{SLUG}-eng-1", THIRD, "for an empty seat")

    action = master_scale._retire(SLUG, store, THIRD, None, NOW)

    assert action == f"closed empty master seat {THIRD}"
    assert inbox.get(waiting.id).address == LEAD and inbox.get(waiting.id).state == "pending"


def test_a_finished_master_that_left_its_seat_is_replaced(swarm):
    store, runtime = swarm
    master_scale.set_rule(store.redis, SLUG, "")
    masters.MasterSeats(store.redis).set_count(SLUG, 2)
    store.put_agent(SLUG, AgentRecord(f"{SLUG}-master-2", MASTER, "master-2", seat=SECOND, state="finished"))

    actions = run(store, runtime)

    [second] = extras(store)
    assert actions == [f"spawned master {second.name} in seat {SECOND}"]


def test_a_failed_master_launch_leaves_no_record_behind(swarm):
    store, runtime = swarm
    master_scale.set_rule(store.redis, SLUG, "")
    masters.MasterSeats(store.redis).set_count(SLUG, 2)
    runtime.fail = True

    actions = run(store, runtime)

    assert len(actions) == 1 and actions[0].startswith(f"master spawn failed in seat {SECOND}: ")
    assert [a.name for a in store.agents(SLUG)] == [LEAD_NAME]


def test_only_a_master_record_in_the_lead_seat_is_the_lead():
    assert masters.is_lead(SLUG, AgentRecord(LEAD_NAME, MASTER, MASTER, seat=LEAD))
    assert masters.is_lead(SLUG, AgentRecord(LEAD_NAME, MASTER, MASTER))
    assert not masters.is_lead(SLUG, AgentRecord("e", "eng", "t1"))
    assert not masters.is_lead(SLUG, AgentRecord("m2", MASTER, "master-2", seat=SECOND))


def test_a_seat_without_phases_is_told_it_owns_none_yet():
    text = prompt.build(SLUG, "/repo", MASTER, "m2", {"id": "master-2", "seat": SECOND, "phases": [], "masters": 2})
    assert "you own: none yet." in text


def test_the_tick_runs_the_scaling_pass_with_its_swarm_and_ledger_document(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 20, 0, state="running"))
    runtime, calls = FakeRuntime(), []
    monkeypatch.setattr(master_scale, "run", lambda *args: calls.append(args) or [])

    tick_module.tick("sw", store, FakeLedger([{"id": "t1", "lane": "eng"}]), runtime, NOW)

    [(slug, config, passed_store, passed_runtime, doc, now)] = calls
    assert (slug, config.slug, passed_store, passed_runtime, now) == ("sw", "sw", store, runtime, NOW)
    assert [t["id"] for t in doc["tasks"]] == ["t1"]


def test_an_extra_master_handoff_through_the_swarm_command_reaches_the_lead(swarm):
    store, runtime = swarm
    _, _, fourth = asked(swarm)
    text = "# Handoff v2\n## Intent\nHand master seat four to the lead.\n<!-- handoff complete -->\n"

    cli._hand_off(store, SLUG, fourth, text, "exit", FakeLedger([]))
    runtime.live.discard(fourth.name)
    run(store, runtime, NOW + 2 * MINUTE + HOLD)

    forwarded = [i.text for i in InboxStore(store.redis).pending_items(LEAD) if i.sender == "swarm"]
    assert forwarded == [master_scale.HANDED.format(address=FOURTH, slug=SLUG) + text]


def test_a_launched_seat_is_primed_with_its_seat_handoff(swarm):
    store, runtime = swarm
    store.put_handoff(SLUG, "master-2", "# Handoff v2\nseat two notes", seat=SECOND)
    grown(swarm)

    name, task = runtime.masters[0]

    assert task["handoff"] == "# Handoff v2\nseat two notes"
    assert "seat two notes" in prompt.build(SLUG, "/repo", MASTER, name, task)
    assert store.handoff(SLUG, "master-2") == ""


def test_growing_from_two_to_four_seats_keeps_every_phase_the_new_seats_do_not_take(swarm):
    store, runtime = swarm
    staffed(store, 10)
    run(store, runtime)
    before = owners(store)
    staffed(store, 20)

    run(store, runtime, NOW + MINUTE)

    after = owners(store)
    assert Counter(after.values()) == {LEAD: 4, SECOND: 4, THIRD: 4, FOURTH: 4}
    assert all(after[p] == before[p] for p in PHASES if after[p] in (LEAD, SECOND))


def test_one_agent_per_master_is_a_rule():
    assert master_scale.rule_of("1") == "1"


def test_the_rule_and_measurements_live_in_this_swarms_masters_hash(swarm):
    store, runtime = swarm
    key = masters.MasterSeats(store.redis).key(SLUG)
    assert store.redis.hget(key, master_scale.RULE) == FIXTURE["rule"]
    grown(swarm)
    assert master_scale.measurements(store.redis, SLUG) == {"master_seat_count": 4, "master_retire_seconds": []}


def test_the_count_change_is_posted_at_the_tick_time(swarm):
    store, runtime = swarm
    staffed(store, 10)
    run(store, runtime, NOW)
    [post] = posts(store)
    assert post.at == NOW / 1000


def test_launched_extra_masters_are_recorded_working(swarm):
    seated = grown(swarm)
    assert [a.state for a in seated] == ["working"] * 3


def test_the_launch_hands_the_swarm_config_to_the_runtime(swarm):
    store, runtime = swarm
    seen = []
    spawn = runtime.spawn
    runtime.spawn = lambda config, lane, name, task: seen.append(config) or spawn(config, lane, name, task)
    grown(swarm)
    assert seen == [store.config(SLUG)] * 3


def test_a_recorded_master_that_is_not_live_still_holds_its_seat(swarm):
    store, runtime = swarm
    master_scale.set_rule(store.redis, SLUG, "")
    masters.MasterSeats(store.redis).set_count(SLUG, 2)
    store.put_agent(SLUG, AgentRecord(f"{SLUG}-master-2", MASTER, "master-2", seat=SECOND))
    assert run(store, runtime) == []


def test_a_full_runtime_holds_the_master_launch(swarm):
    store, runtime = swarm
    master_scale.set_rule(store.redis, SLUG, "")
    masters.MasterSeats(store.redis).set_count(SLUG, 2)
    runtime.full = True
    assert run(store, runtime) == ["no session slot for the master, waiting"]


def test_a_full_host_holds_the_master_launch(swarm, monkeypatch):
    store, runtime = swarm
    master_scale.set_rule(store.redis, SLUG, "")
    masters.MasterSeats(store.redis).set_count(SLUG, 2)
    asked = []
    monkeypatch.setattr(tick_module, "_host_full", lambda slug, s, now: asked.append((slug, now)) or "memory low")
    assert run(store, runtime) == ["holding the master spawn: memory low"]
    assert asked == [(SLUG, NOW)] and runtime.masters == []


def test_a_regrowth_clears_the_retire_state_and_names_why(swarm):
    store, runtime = swarm
    inbox = InboxStore(store.redis)
    seated = asked(swarm)
    asks = [item for agent in seated for item in inbox.pending_items(agent.seat)]
    staffed(store, 20)
    run(store, runtime, NOW + 2 * MINUTE + HOLD)
    assert store.redis.hget(masters.MasterSeats(store.redis).key(SLUG), master_scale.RETIRING) is None
    assert [inbox.history(i.id)[-1]["reason"] for i in asks] == [
        f"the swarm grew again, so seat {a.seat} stays" for a in seated
    ]


def test_a_regrowth_leaves_an_ask_that_moved_elsewhere(swarm):
    store, runtime = swarm
    inbox = InboxStore(store.redis)
    _, _, fourth = asked(swarm)
    [ask] = inbox.pending_items(FOURTH)
    inbox.redirect(ask.id, "swarm", LEAD, "moved by hand")
    staffed(store, 20)
    run(store, runtime, NOW + 2 * MINUTE + HOLD)
    assert (inbox.get(ask.id).address, inbox.get(ask.id).state) == (LEAD, "pending")


def test_a_retired_seat_names_why_it_moved_and_withdrew_items_and_skips_closed_ones(swarm):
    store, _ = swarm
    inbox = InboxStore(store.redis)
    done = inbox.send(f"{SLUG}-eng-1", THIRD, "already answered")
    inbox.withdraw(done.id, "swarm", "answered elsewhere")
    waiting = inbox.send(f"{SLUG}-eng-1", THIRD, "still open")
    ask = inbox.send("swarm", THIRD, "please retire")

    master_scale._retire(SLUG, store, THIRD, [NOW, ask.id], NOW + MINUTE)

    assert inbox.get(waiting.id).address == LEAD
    assert inbox.history(waiting.id)[-1]["reason"] == f"master seat {THIRD} retired"
    assert inbox.history(ask.id)[-1]["reason"] == f"master seat {THIRD} retired"


def test_an_extra_seat_that_emptied_before_its_ask_is_closed_and_the_count_falls_past_it(swarm):
    store, runtime = swarm
    _, _, fourth = grown(swarm)
    store.drop_agent(SLUG, fourth.name)
    runtime.live.discard(fourth.name)
    staffed(store, 5)
    run(store, runtime, NOW + MINUTE)

    actions = run(store, runtime, NOW + MINUTE + HOLD)

    assert f"closed empty master seat {FOURTH}" in actions and count(store) == 3


def test_a_live_master_above_the_count_never_keeps_the_count_up(swarm):
    store, runtime = swarm
    seated = asked(swarm)
    above = AgentRecord(f"{SLUG}-master-5", MASTER, "master-5", seat=f"master-5@{SLUG}")
    store.put_agent(SLUG, above)
    runtime.live.add(above.name)
    for agent in seated:
        hand_off(store, runtime, agent)

    run(store, runtime, NOW + 2 * MINUTE + HOLD)

    assert count(store) == 1


def test_an_announcement_names_a_seat_that_owns_no_phase():
    text = master_scale.announcement(SLUG, 2, 6, {"p1": LEAD})
    assert text == f"Master seats are now 2 for 6 working agents. Phase owners: {LEAD} owns p1; {SECOND} owns no phase."


def test_the_seat_lines_tell_an_extra_master_its_seat_phases_channel_and_retirement():
    swarm = f"agentihooks swarm {SLUG}"
    assert masters.seat_lines(SLUG, {"seat": SECOND, "masters": 2}) == [
        f"You hold master seat {SECOND}, one of 2 masters of swarm {SLUG}. The lead seat {LEAD} answers the operator "
        "and his chat; you act on the inbox items for the phases and hives you own: none yet.",
        f"The tick assigns phases by code and posts every change on the masters channel. Read it with {swarm} "
        f'masters-channel read and coordinate with the other masters with {swarm} masters-channel say "<text>".',
        f"When the swarm shrinks the tick asks you to retire: write your Handoff v2 for the lead master, run {swarm} "
        "handoff <doc> and stop.",
    ]


def test_a_finished_lead_is_no_master_to_notify(swarm):
    from scripts.swarm import control_notifications

    store, _ = swarm
    store.put_agent(SLUG, AgentRecord(LEAD_NAME, MASTER, MASTER, pane_id="pm1", seat=LEAD, state="finished"))
    assert control_notifications.master(store, SLUG) is None


EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-MST-03"


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    from tests.sv2_mst03_cases import run_case

    first, second = run_case(case), run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    committed = json.loads((EVIDENCE / f"{case}-result.json").read_text(encoding="utf-8"))
    assert committed == {"case": f"T-SV2-MST-03-{case.upper()}", "independent_runs": 2, "observed": first}, json.dumps(
        first, indent=2, sort_keys=True
    )


def test_the_manifest_names_the_hash_of_every_case_input():
    import hashlib

    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    root = Path(__file__).parents[1]
    assert manifest["inputs"] == {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in ["tests/fixtures/swarm_v2/master-scaling.json"]
    }
    assert manifest["cases"] == ["T-SV2-MST-03-A", "T-SV2-MST-03-B", "T-SV2-MST-03-C"]


def test_the_package_record_carries_its_completion_evidence():
    record = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    assert (record["package"], record["package_complete"], record["integration_gate"]) == ("SV2-MST-03", False, "G10")
    assert set(record["states"]) == {"observed", "committed", "externally_verified"}
    for name in ("interfaces", "supported_versions", "authoritative_objects", "remaining_limitations", "validation"):
        assert record[name], name
    assert set(record["measurements"]) == {"master_seat_count", "master_retire_seconds"}
    assert record["cases"] == {
        **{case: f"evidence/SV2-MST-03/{case}-result.json" for case in "abc"},
        "manifest": "evidence/SV2-MST-03/manifest.json",
    }
    source = Path(__file__).read_text(encoding="utf-8")
    path, name = record["rollback_rehearsal"]["test"].split("::")
    assert path == "tests/test_swarm_v2_master_scale.py"
    assert f"\ndef {name}(" in source
    for obligation in record["obligations"].values():
        assert obligation["met_by"]
        for test in obligation["tests"]:
            path, function = test.split("::")
            assert path == "tests/test_swarm_v2_master_scale.py"
            assert f"\ndef {function}(" in source, test
