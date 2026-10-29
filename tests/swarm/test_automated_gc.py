from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm.store import AgentRecord, RedisStore

SLUG = "proof-323133-t1701-2"
NAME = "master@323133-0100"
SEAT = f"master@{SLUG}"
START = 1000
LIMIT = 30 * 60 * 1000


@pytest.fixture
def world():
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    inbox = InboxStore(redis)
    agent = AgentRecord(
        NAME,
        "master",
        "master",
        seat=SEAT,
        started_at=START,
        profile_decision={"automated": {"swarm": SLUG, "seat": SEAT}},
    )
    store.put_agent(SLUG, agent)
    runtime = Mock()
    runtime.observe.return_value = SimpleNamespace(state="idle", typed=False, prompt_title="", provider_error="")
    runtime.retire.return_value = True
    runtime.nudge.return_value = None
    ledger = Mock()
    return store, inbox, agent, runtime, ledger


def sweep(world, at):
    from scripts.swarm import automated_gc

    store, _, _, runtime, ledger = world
    return automated_gc.run(SLUG, store, ledger, runtime, at)


def question(world):
    _, inbox, agent, _, _ = world
    return next(item for item in inbox.inbox(agent.name) if item.state not in CLOSED)


def answer(world, text):
    _, inbox, agent, _, _ = world
    return inbox.reply(question(world).id, agent.name, text)


def test_only_explicit_launch_ownership_is_collectable(world):
    from scripts.swarm import automated_gc

    _, _, agent, _, _ = world
    assert automated_gc.owned(SLUG, agent)
    assert not automated_gc.owned("another", agent)
    assert not automated_gc.owned(SLUG, replace(agent, seat="another"))
    assert not automated_gc.owned(SLUG, replace(agent, profile_decision={}))


def test_thirty_minute_probe_records_real_inbox_answer_once(world):
    store, inbox, agent, runtime, _ = world
    assert sweep(world, START + LIMIT - 1) == []
    assert inbox.inbox(agent.name) == []
    assert sweep(world, START + LIMIT)
    item = question(world)
    assert "nothing to do" in item.text
    assert item.ref == "automated-activity"
    assert item.task == agent.task
    runtime.nudge.assert_called_once_with(
        agent,
        f"Read activity question {item.id} with agentihooks msg read, then answer it with "
        "agentihooks msg reply. Reply exactly nothing to do only when you have no work.",
    )
    assert sweep(world, START + LIMIT + 1) == []
    reply = answer(world, "nothing to do")
    assert sweep(world, START + LIMIT + 2) == [f"recorded activity answer from {agent.name}"]
    assert sweep(world, START + LIMIT + 3) == []
    from scripts.swarm import automated_gc

    state = automated_gc.state(store, SLUG, agent)
    assert state["idle_answers"] == 1
    assert state["request"] == ""
    assert inbox.get(reply.id).reason == "done: activity answer recorded"
    assert state["answers"] == [{"id": reply.id, "text": "nothing to do", "at": START + LIMIT + 2}]
    runtime.retire.assert_not_called()


def test_third_distinct_idle_answer_retires_owned_pane_and_records_exit(world):
    store, _, agent, runtime, ledger = world
    release = Mock(wraps=store.release)
    store.release = release
    for attempt in range(1, 4):
        at = START + attempt * LIMIT
        sweep(world, at)
        answer(world, "nothing to do")
        actions = sweep(world, at + 1)
        assert actions[0] == f"recorded activity answer from {agent.name}"
        if attempt == 3:
            assert actions[1] == f"collected automated agent {agent.name} after three idle answers"
        if attempt < 3:
            runtime.retire.assert_not_called()
    runtime.retire.assert_called_once_with(agent)
    assert store.agents(SLUG) == []
    ledger.notify.assert_called_once_with(SLUG, "An automated agent ended after three nothing to do answers.")
    release.assert_called_once_with(SLUG, agent.task, agent.name)


@pytest.mark.parametrize("text", ["working: reviewing a pull request", "I have work", "NOTHING TO DO later"])
def test_other_answers_reset_idle_passes(world, text):
    from scripts.swarm import automated_gc

    store, _, agent, runtime, _ = world
    sweep(world, START + LIMIT)
    answer(world, "nothing to do")
    sweep(world, START + LIMIT + 1)
    sweep(world, START + LIMIT * 2)
    answer(world, text)
    sweep(world, START + LIMIT * 2 + 1)
    assert automated_gc.state(store, SLUG, agent)["idle_answers"] == 0
    runtime.retire.assert_not_called()


def test_untagged_operator_sessions_are_never_observed_or_terminated(world):
    store, inbox, agent, runtime, ledger = world
    store.put_agent(SLUG, replace(agent, profile_decision={}))
    assert sweep(world, START + LIMIT * 100) == []
    assert inbox.inbox(agent.name) == []
    runtime.observe.assert_not_called()
    runtime.retire.assert_not_called()
    ledger.notify.assert_not_called()


def test_unanswered_probe_never_counts_idle_passes(world):
    from scripts.swarm import automated_gc

    store, inbox, agent, runtime, _ = world
    for attempt in range(1, 6):
        sweep(world, START + LIMIT * attempt)
    assert len(inbox.inbox(agent.name)) == 1
    assert automated_gc.state(store, SLUG, agent)["idle_answers"] == 0
    runtime.retire.assert_not_called()


def test_retirement_failure_keeps_agent_for_retry_without_another_idle_pass(world):
    store, _, agent, runtime, ledger = world
    runtime.retire.return_value = False
    for attempt in range(1, 4):
        at = START + attempt * LIMIT
        sweep(world, at)
        answer(world, "nothing to do")
        sweep(world, at + 1)
    assert store.agents(SLUG) == [agent]
    ledger.notify.assert_not_called()
    runtime.retire.return_value = True
    sweep(world, START + LIMIT * 3 + 2)
    assert store.agents(SLUG) == []
    assert runtime.retire.call_count == 2


@pytest.mark.parametrize(
    "changed", [dict(state="working"), dict(typed=True), dict(prompt_title="Permission"), dict(provider_error="outage")]
)
def test_busy_or_held_panes_are_not_probed(world, changed):
    _, _, _, runtime, _ = world
    runtime.observe.return_value = SimpleNamespace(
        **(dict(state="idle", typed=False, prompt_title="", provider_error="") | changed)
    )
    assert sweep(world, START + LIMIT) == []
    runtime.retire.assert_not_called()


@pytest.mark.parametrize("lane,seat", [("eng", "eng-1"), ("master", "master")])
def test_real_automated_launch_persists_swarm_and_seat(tmp_path, lane, seat):
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.tick import placed_record
    from tests.swarm.profile_fixture import validated

    def launch(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=launch, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug=SLUG, repo=str(tmp_path), code="323133", compact_limit=0, lanes={}, autonomy="delegate"
    )
    address = f"{seat}@{SLUG}"
    placed = runtime.spawn(config, lane, NAME, {"id": "t1", "title": "work", "seat": address})
    record = placed_record(AgentRecord(NAME, lane, "t1", seat=address), placed)
    assert record.profile_decision["automated"] == {"swarm": SLUG, "seat": address}


def test_stopped_proof_swarm_still_collects_its_automated_master(world, monkeypatch):
    from scripts.swarm import lifetime, tick

    store, inbox, agent, runtime, ledger = world
    config = SimpleNamespace(state="stopped")
    monkeypatch.setattr(store, "ensure_code", lambda _: config)
    monkeypatch.setattr(tick, "_woken", lambda *_: False)
    monkeypatch.setattr(tick, "_close_space", lambda *_: [])
    monkeypatch.setattr(tick, "_bin_closed", lambda *_: [])
    ledger.state = lambda _: {"tasks": []}

    def receive(actual, text):
        assert actual == agent
        assert question(world).id in text
        inbox.reply(question(world).id, agent.name, "nothing to do")

    runtime.nudge.side_effect = receive
    for attempt in range(1, 4):
        at = START + attempt * LIMIT
        tick.tick(SLUG, store, ledger, runtime, at)
        tick.tick(SLUG, store, ledger, runtime, at + 1)
    runtime.retire.assert_called_once_with(agent)
    assert runtime.nudge.call_count == 3
    assert store.agents(SLUG) == []
    assert lifetime.sleeping(SLUG, store, {})
    ledger.notify.assert_called_once()


def test_tagged_worker_waits_for_explicit_idle_answers_instead_of_tick_limit(world, monkeypatch):
    from scripts.swarm import tick

    store, _, agent, runtime, ledger = world
    agent = replace(agent, lane="eng", task="t1", idle_ticks=tick.IDLE_KILL_TICKS)
    store.put_agent(SLUG, agent)
    monkeypatch.setattr(tick, "_refusal_due", lambda *_: False)
    monkeypatch.setattr(tick, "_stuck_on_provider", lambda *_: False)
    monkeypatch.setattr(tick, "_operator_at_pane", lambda *_: False)
    monkeypatch.setattr(tick.ledger_probe, "holding", lambda *_: False)
    tick._watch_idle(SLUG, store, ledger, runtime, {}, agent, START + LIMIT)
    runtime.retire.assert_not_called()


def test_distributed_launch_tags_admitted_worker_before_handoff():
    from scripts.swarm_v2.launch import DistributedLaunch

    controller = Mock()
    admitted = AgentRecord(NAME, "eng", "t1", seat=SEAT, profile_decision={"profile": "engineer"})
    controller.admit.side_effect = lambda agent, previous: agent
    launch = object.__new__(DistributedLaunch)
    launch.controller = controller
    launch.slug = SLUG
    launch._place = Mock()
    request, terms = Mock(), SimpleNamespace(account="account")
    launch.spawn(request, admitted, terms, "", "opening")
    record = controller.admit.call_args.args[0]
    assert record.account == "account"
    assert record.profile_decision == {"profile": "engineer", "automated": {"swarm": SLUG, "seat": SEAT}}
    launch._place.assert_called_once_with(request, record, terms, launch.end_refused, "opening")


def test_new_activity_and_named_wait_postpone_the_probe(world):
    from scripts.swarm import idle

    store, inbox, agent, runtime, _ = world
    idle.beat(store.redis, SLUG, agent.name, "working", START + LIMIT)
    assert sweep(world, START + LIMIT + 1) == []
    assert inbox.inbox(agent.name) == []
    runtime.retire.assert_not_called()


def test_an_idle_reply_does_not_retire_a_now_busy_pane(world):
    _, _, _, runtime, _ = world
    for attempt in range(1, 4):
        at = START + attempt * LIMIT
        sweep(world, at)
        answer(world, "nothing to do")
        if attempt == 3:
            runtime.observe.return_value.state = "working"
        sweep(world, at + 1)
    runtime.retire.assert_not_called()


def test_failed_probe_delivery_retries_the_same_question(world):
    _, inbox, agent, runtime, _ = world
    runtime.nudge.return_value = "transport unavailable"
    sweep(world, START + LIMIT)
    request = question(world)
    runtime.nudge.return_value = None
    sweep(world, START + LIMIT + 1)
    assert runtime.nudge.call_count == 2
    assert question(world).id == request.id
    assert len(inbox.inbox(agent.name)) == 1
    sweep(world, START + LIMIT + 2)
    assert runtime.nudge.call_count == 2


def test_collected_master_sleeps_until_new_task_or_operator_inbox(world):
    from scripts.swarm import lifetime, tick

    store, inbox, agent, _, ledger = world
    tick._collected(SLUG, store, ledger, {}, agent)
    assert lifetime.sleeping(SLUG, store, {})
    assert not lifetime.sleeping(SLUG, store, {"new-task": {}})
    store.redis.set(store.key(SLUG, "master-retired-tasks"), "[]")
    inbox.send("operator", agent.seat, "New work")
    assert not lifetime.sleeping(SLUG, store, {})


@pytest.mark.parametrize("first_state", ["untagged", "busy", "collected", "ready"])
def test_collection_continues_to_later_agents(world, monkeypatch, first_state):
    from scripts.swarm import automated_gc

    store, inbox, agent, runtime, ledger = world
    later = replace(agent, name="master@323133-9999")
    first = replace(agent, profile_decision={}) if first_state == "untagged" else agent
    monkeypatch.setattr(store, "agents", lambda slug: [first, later] if slug == SLUG else [])
    if first_state == "busy":
        runtime.observe.side_effect = lambda actual: SimpleNamespace(
            state="working" if actual == first else "idle", typed=False, prompt_title="", provider_error=""
        )
    if first_state == "collected":
        record = automated_gc.state(store, SLUG, first)
        record["idle_answers"] = 3
        automated_gc._save(store, SLUG, first, record)
    actions = sweep(world, START + LIMIT)
    assert len(inbox.inbox(later.name)) == 1
    if first_state in {"collected", "ready"}:
        assert len(actions) == 2
    if first_state == "collected":
        runtime.retire.assert_called_once_with(first)
        assert actions[0] == f"collected automated agent {first.name} after three idle answers"
    else:
        runtime.retire.assert_not_called()


@pytest.mark.parametrize("swarm_state", ["stopped", "paused"])
@pytest.mark.parametrize("idle_answers", [0, 3])
def test_tick_collects_with_the_current_ledger_rows(world, monkeypatch, swarm_state, idle_answers):
    from scripts.swarm import automated_gc, tick

    store, _, agent, runtime, ledger = world
    config = SimpleNamespace(state=swarm_state, engine=tick.EVENTS_ENGINE)
    rows = {"work": {"id": "work", "state": "done"}}
    ledger.state.return_value = {"tasks": list(rows.values())}
    monkeypatch.setattr(store, "ensure_code", lambda slug: config if slug == SLUG else None)
    monkeypatch.setattr(tick, "_woken", lambda *_: False)
    monkeypatch.setattr(tick, "_bin_closed", lambda *_: [])
    monkeypatch.setattr(tick, "skip_refused", lambda *_: [])
    monkeypatch.setattr(tick, "skip_dropped", lambda *_: [])
    monkeypatch.setattr(tick.freeze, "watched", lambda slug, actual_store, actual_ledger, doc: doc)
    monkeypatch.setattr(tick.lifetime, "sleeping", lambda *_: True)
    monkeypatch.setattr(tick.timing, "call", lambda fn, *args: fn(*args) if fn == ledger.state else [])
    collected = Mock()
    monkeypatch.setattr(tick, "_collected", collected)
    record = automated_gc.state(store, SLUG, agent)
    record["idle_answers"] = idle_answers
    automated_gc._save(store, SLUG, agent, record)
    actions = tick.tick(SLUG, store, ledger, runtime, START + LIMIT)
    ledger.state.assert_called_once_with(SLUG)
    if idle_answers == 3:
        assert actions == [f"collected automated agent {agent.name} after three idle answers"]
        runtime.retire.assert_called_once_with(agent)
        collected.assert_called_once_with(SLUG, store, ledger, rows, agent)
    else:
        assert actions == [f"asked automated agent {agent.name} what it is doing"]
        runtime.retire.assert_not_called()
        collected.assert_not_called()


def test_activity_state_is_persisted_in_its_swarm_namespace(world):
    import json

    from scripts.swarm import automated_gc

    store, _, agent, runtime, _ = world
    expected = {"idle_answers": 0, "answers": [], "request": "", "last_probe": 0}
    assert automated_gc.state(store, SLUG, agent) == expected
    runtime.nudge.return_value = "not delivered"
    sweep(world, START + LIMIT)
    record = json.loads(store.redis.get(store.key(SLUG, "automated-gc", f"{NAME}:{START}")))
    assert record["delivered"] is False
    assert record["request"] == question(world).id
    assert record["last_probe"] == START + LIMIT


def test_activity_history_retains_only_the_last_three_answers(world):
    from scripts.swarm import automated_gc

    store, _, agent, _, _ = world
    replies = []
    for attempt in range(1, 5):
        at = START + attempt * LIMIT
        sweep(world, at)
        reply = answer(world, f"working on item {attempt}")
        sweep(world, at + 1)
        replies.append({"id": reply.id, "text": reply.text, "at": at + 1})
    assert automated_gc.state(store, SLUG, agent)["answers"] == replies[-3:]


@pytest.mark.parametrize("wrong", ["reference", "sender"])
def test_unrelated_activity_answers_are_not_recorded(world, wrong):
    from scripts.swarm import automated_gc

    _, _, agent, _, _ = world
    record = {"request": "question", "answers": [], "idle_answers": 0}
    inbox = Mock()
    inbox.worlds.return_value = [inbox]
    inbox.redis.exists.return_value = True
    inbox.get.side_effect = [
        SimpleNamespace(id="question", reason=automated_gc.REPLY_PREFIX + "answer"),
        SimpleNamespace(ref="other" if wrong == "reference" else "question", sender="stranger"),
    ]
    inbox.acts_for.return_value = wrong != "sender"
    assert not automated_gc._answer(inbox, agent, record, START)
    assert record == {"request": "question", "answers": [], "idle_answers": 0}
    inbox.close.assert_not_called()


@pytest.mark.parametrize("hold", ["heartbeat", "wait", "outage"])
def test_activity_holds_prevent_collection(world, monkeypatch, hold):
    from scripts.swarm import automated_gc

    store, _, agent, runtime, _ = world
    at = START + LIMIT
    held = Mock(return_value=hold == "outage")
    monkeypatch.setattr(automated_gc.outage, "held", held)
    monkeypatch.setattr(
        automated_gc.idle, "heartbeat", lambda *_: {"state": "working", "at": at} if hold == "heartbeat" else None
    )
    monkeypatch.setattr(automated_gc.idle, "wait", lambda *_: {"until": at + 10} if hold == "wait" else None)
    assert not automated_gc._ready(store, SLUG, runtime, agent, at)
    held.assert_called_once_with(store, SLUG, agent.name)
    if hold == "outage":
        runtime.observe.assert_not_called()
    else:
        runtime.observe.assert_called_once_with(agent)


@pytest.mark.parametrize("source", ["heartbeat", "prompt"])
def test_recent_idle_activity_postpones_the_next_question(world, monkeypatch, source):
    from scripts.swarm import idle

    store, inbox, agent, runtime, _ = world
    at = START + LIMIT
    if source == "heartbeat":
        idle.beat(store.redis, SLUG, agent.name, "idle", at)
    else:
        monkeypatch.setattr(idle, "last_prompt", lambda *_: at)
    assert sweep(world, at + 1) == []
    assert inbox.inbox(agent.name) == []
    runtime.retire.assert_not_called()


@pytest.mark.parametrize("push,owner", [(True, False), (False, True)])
def test_activity_question_uses_the_existing_inbox_transport(world, monkeypatch, push, owner):
    from scripts.inbox.dispatch import Dispatcher
    from scripts.swarm import automated_gc, capabilities

    store, inbox, agent, runtime, _ = world
    monkeypatch.setattr(capabilities, "of_agent", lambda _: SimpleNamespace(push_inbox=push))
    monkeypatch.setattr(Dispatcher, "owner", lambda self, name: owner)
    sweep(world, START + LIMIT)
    assert len(inbox.inbox(agent.name)) == 1
    assert automated_gc.state(store, SLUG, agent)["delivered"] is True
    assert sweep(world, START + LIMIT + 1) == []
    runtime.nudge.assert_not_called()


def test_collected_worker_forwards_its_ledger_to_claim_cleanup(world, monkeypatch):
    from scripts.swarm import tick

    store, _, agent, _, ledger = world
    worker = replace(agent, lane="eng", task="work")
    rows = {"work": {"id": "work"}}
    drop = Mock(return_value="task reopened")
    monkeypatch.setattr(tick, "_drop", drop)
    assert tick._collected(SLUG, store, ledger, rows, worker) == "task reopened"
    drop.assert_called_once_with(SLUG, store, ledger, rows, worker)


def test_legacy_agent_without_a_start_timestamp_is_probed_at_thirty_minutes(world):
    store, inbox, agent, runtime, _ = world
    store.put_agent(SLUG, replace(agent, started_at=0))
    assert sweep(world, LIMIT - 1) == []
    assert sweep(world, LIMIT) == [f"asked automated agent {agent.name} what it is doing"]
    assert len(inbox.inbox(agent.name)) == 1
    runtime.retire.assert_not_called()


def test_hive_hosted_agent_answer_is_read_from_its_member_world(world):
    import fakeredis

    from scripts.hive import auth
    from scripts.swarm import automated_gc
    from scripts.swarm.store import SwarmConfig

    _, _, _, runtime, ledger = world
    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    store.create(SwarmConfig(SLUG, "agentihooks", 1, 0))
    redis.hset(f"{auth.PREFIX}:member:m1", mapping={"name": "m1", "ledger": "digest"})
    redis.sadd(auth.INDEX, "m1")
    seat = f"eng-1@{SLUG}"
    name = store.next_name(SLUG, "eng")
    decision = {"automated": {"swarm": SLUG, "seat": seat}}
    store.start_execution(
        SLUG, AgentRecord(name, "eng", "t1", seat=seat, hive="m1", started_at=START, profile_decision=decision)
    )
    store.seats.occupy(seat, name, 1)
    controller, member = InboxStore(redis), InboxStore(redis, world="m1")
    asked = [f"asked automated agent {name} what it is doing"]
    assert automated_gc.run(SLUG, store, ledger, runtime, START + LIMIT) == asked
    question = next(item for item in member.inbox(name) if item.state not in CLOSED)
    assert not redis.exists(controller.key("item", question.id))
    reply = member.reply(question.id, name, "nothing to do")
    recorded = [f"recorded activity answer from {name}"]
    assert automated_gc.run(SLUG, store, ledger, runtime, START + LIMIT + 1) == recorded
    agent = next(a for a in store.agents(SLUG) if a.name == name)
    state = automated_gc.state(store, SLUG, agent)
    assert state["idle_answers"] == 1
    assert state["request"] == ""
    held = next(w for w in controller.worlds() if redis.exists(w.key("item", reply.id)))
    assert held.get(reply.id).reason == "done: activity answer recorded"


def test_a_question_gone_from_every_world_is_asked_again(world):
    from scripts.swarm import automated_gc

    store, inbox, agent, _, _ = world
    sweep(world, START + LIMIT)
    gone = question(world)
    inbox.redis.delete(inbox.key("item", gone.id))
    at = START + LIMIT * 2
    assert sweep(world, at) == [f"asked automated agent {agent.name} what it is doing"]
    record = automated_gc.state(store, SLUG, agent)
    assert record["request"] == question(world).id
    assert record["request"] != gone.id
    assert record["last_probe"] == at


def test_a_question_whose_answer_is_gone_is_asked_again(world):
    from scripts.swarm import automated_gc

    store, inbox, agent, _, _ = world
    sweep(world, START + LIMIT)
    first = question(world).id
    reply = answer(world, "nothing to do")
    inbox.redis.delete(inbox.key("item", reply.id))
    at = START + LIMIT * 2
    assert sweep(world, at) == [f"asked automated agent {agent.name} what it is doing"]
    record = automated_gc.state(store, SLUG, agent)
    assert record["idle_answers"] == 0
    assert record["request"] == question(world).id
    assert record["request"] != first
    assert record["last_probe"] == at
