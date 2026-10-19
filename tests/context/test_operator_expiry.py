from unittest.mock import Mock

import pytest

from hooks.context import operator_mode

pytestmark = pytest.mark.unit


@pytest.fixture
def clocked_scheduler(monkeypatch):
    import fakeredis

    from scripts.scheduler import Scheduler

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda: redis)
    monkeypatch.setattr("scripts.scheduler.service.ensure_running", Mock(return_value=True))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    monkeypatch.setattr("scripts.inbox.cli.registered_name", lambda: "")
    monkeypatch.delenv("AGENTIHOOKS_OPERATOR_TIMEOUT_SEC", raising=False)
    return Scheduler(redis)


def test_operator_on_expires_without_another_prompt_and_notifies_only_its_session(clocked_scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler import Scheduler

    operator_mode.observe("pane", "operator on", True, now=100)
    operator_mode.observe("other", "operator on", True, now=200)
    assert operator_mode.present("pane", now=399)
    assert clocked_scheduler.run_due(now=399) == 0
    restarted = Scheduler(clocked_scheduler.redis)
    assert restarted.run_due(now=400) == 1
    assert not operator_mode.present("pane", now=400)
    state = operator_mode._load("pane")
    assert state["on"] is False
    assert state["turn_at"] is None
    assert operator_mode.present("other", now=400)
    notices = InboxStore(clocked_scheduler.redis).inbox("pane")
    assert [item.text for item in notices] == [
        "Operator off: no typed prompt arrived before the operator timer expired."
    ]
    assert InboxStore(clocked_scheduler.redis).inbox("other") == []
    assert restarted.run_due(now=401) == 0
    assert len(InboxStore(clocked_scheduler.redis).inbox("pane")) == 1


@pytest.mark.parametrize(("prompt", "fires"), [("continue", 1), ("operator off", 0)])
def test_a_typed_prompt_restarts_or_cancels_the_session_timer(clocked_scheduler, prompt, fires):
    from scripts.inbox.store import InboxStore

    operator_mode.observe("pane", "operator on", True, now=100)
    operator_mode.observe("pane", prompt, True, now=200)
    operator_mode.observe("pane", "automated wake", False, now=300)
    assert clocked_scheduler.run_due(now=400) == 0
    assert operator_mode.present("pane", now=400) is bool(fires)
    assert InboxStore(clocked_scheduler.redis).inbox("pane") == []
    assert clocked_scheduler.run_due(now=500) == fires
    assert not operator_mode.present("pane", now=500)
    assert len(InboxStore(clocked_scheduler.redis).inbox("pane")) == fires


def test_operator_off_without_a_timer_keeps_the_session_off(clocked_scheduler):
    from scripts.inbox.store import InboxStore

    operator_mode.observe("pane", "operator off", True, now=100)
    assert not operator_mode.present("pane", now=100)
    assert operator_mode._load("pane") == {"on": False, "at": 100, "turn_at": None}
    assert clocked_scheduler.run_due(now=400) == 0
    assert InboxStore(clocked_scheduler.redis).inbox("pane") == []


@pytest.mark.parametrize("seconds", [60, 600])
def test_the_operator_window_is_configurable(clocked_scheduler, monkeypatch, seconds):
    monkeypatch.setenv("AGENTIHOOKS_OPERATOR_TIMEOUT_SEC", str(seconds))
    operator_mode.observe("pane", "operator on", True, now=100)
    assert clocked_scheduler.run_due(now=100 + seconds - 1) == 0
    assert clocked_scheduler.run_due(now=100 + seconds) == 1
    assert not operator_mode.present("pane", now=100 + seconds)


@pytest.mark.parametrize(("prompt", "present"), [("continue", True), ("operator off", False)])
def test_an_already_claimed_expiry_cannot_override_a_newer_prompt(clocked_scheduler, prompt, present):
    from hooks.context.operator_expiry import expire
    from scripts.inbox.store import InboxStore

    operator_mode.observe("pane", "operator on", True, now=100)
    timer_id = operator_mode._load("pane")["timer_id"]
    old_event = clocked_scheduler.get(timer_id).record
    operator_mode.observe("pane", prompt, True, now=200)
    expire(old_event, "old-delivery", clocked_scheduler.redis)
    assert operator_mode.present("pane", now=400) is present
    assert InboxStore(clocked_scheduler.redis).inbox("pane") == []


def test_expiry_retries_after_accepted_delivery_do_not_duplicate_the_notice(clocked_scheduler, monkeypatch):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    deliver = InboxSlot.deliver

    def accepted_before_crash(slot, event, delivery_id, redis):
        deliver(slot, event, delivery_id, redis)
        raise RuntimeError("lost response")

    operator_mode.observe("pane", "operator on", True, now=100)
    monkeypatch.setattr(InboxSlot, "deliver", accepted_before_crash)
    assert clocked_scheduler.run_due(now=400) == 1
    assert not operator_mode.present("pane", now=400)
    monkeypatch.setattr(InboxSlot, "deliver", deliver)
    assert clocked_scheduler.run_due(now=401) == 0
    assert len(InboxStore(clocked_scheduler.redis).inbox("pane")) == 1


def test_expiry_routes_to_the_bound_sessions_inbox_name(clocked_scheduler, monkeypatch):
    from scripts.inbox.store import InboxStore

    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@proof")
    operator_mode.observe("session-uuid", "operator on", True, now=100)
    assert clocked_scheduler.run_due(now=400) == 1
    assert not operator_mode.present("session-uuid", now=400)
    inbox = InboxStore(clocked_scheduler.redis)
    assert len(inbox.inbox("engineer@proof")) == 1
    assert inbox.inbox("session-uuid") == []
