from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore

SLUG = "proof-323133-t1701-2"
NAME = "master@323133-100"
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
    ledger = Mock()
    return store, inbox, agent, runtime, ledger


def sweep(world, at):
    from scripts.swarm import automated_gc

    store, _, _, runtime, ledger = world
    return automated_gc.run(SLUG, store, ledger, runtime, at)


def question(world):
    _, inbox, agent, _, _ = world
    return inbox.inbox(agent.name)[-1]


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
