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
    assert sweep(world, START + LIMIT + 1) == []
    reply = answer(world, "nothing to do")
    sweep(world, START + LIMIT + 2)
    sweep(world, START + LIMIT + 3)
    from scripts.swarm import automated_gc

    state = automated_gc.state(store, SLUG, agent)
    assert state["idle_answers"] == 1
    assert state["answers"] == [{"id": reply.id, "text": "nothing to do", "at": START + LIMIT + 2}]
    runtime.retire.assert_not_called()


def test_third_distinct_idle_answer_retires_owned_pane_and_records_exit(world):
    store, _, agent, runtime, ledger = world
    for attempt in range(1, 4):
        at = START + attempt * LIMIT
        sweep(world, at)
        answer(world, "nothing to do")
        sweep(world, at + 1)
        if attempt < 3:
            runtime.retire.assert_not_called()
    runtime.retire.assert_called_once_with(agent)
    assert store.agents(SLUG) == []
    ledger.notify.assert_called_once_with(SLUG, "An automated agent ended after three nothing to do answers.")


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
