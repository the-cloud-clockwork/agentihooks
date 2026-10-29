from unittest.mock import Mock

import pytest

from hooks.context import operator_mode
from scripts.events.redis_state import RedisStateStore

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
    return Scheduler(RedisStateStore(redis, "scheduler"))


def legacy_timer(scheduler, session_id):
    from hooks.context.operator_expiry import expire

    timer = scheduler.timer(owner=session_id, payload="old expiry notice", delay=300, now=100)
    timer.timeout.connect(expire)
    state = operator_mode._load(session_id)
    state.update(timer_id=timer.id, notice_address=session_id)
    operator_mode._save(session_id, state)
    timer.start(now=100)
    return timer


def test_operator_on_survives_idle_time_without_notifying_its_session(clocked_scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler import Scheduler

    operator_mode.observe("pane", "operator on", True, now=100)
    operator_mode.observe("other", "operator on", True, now=200)
    restarted = Scheduler(RedisStateStore(clocked_scheduler.state.redis, "scheduler"))
    assert restarted.run_due(now=100000) == 0
    assert operator_mode.present("pane", now=100000)
    assert operator_mode.present("other", now=100000)
    assert "timer_id" not in operator_mode._load("pane")
    assert InboxStore(clocked_scheduler.state.redis).inbox("pane") == []
    assert InboxStore(clocked_scheduler.state.redis).inbox("other") == []


@pytest.mark.parametrize(("prompt", "fires"), [("continue", 1), ("operator off", 0)])
def test_a_typed_prompt_cancels_a_legacy_timer_without_expiring_presence(clocked_scheduler, prompt, fires):
    from scripts.inbox.store import InboxStore

    operator_mode.observe("pane", "operator on", True, now=100)
    legacy_timer(clocked_scheduler, "pane")
    operator_mode.observe("pane", prompt, True, now=200)
    operator_mode.observe("pane", "automated wake", False, now=300)
    assert clocked_scheduler.run_due(now=100000) == 0
    assert operator_mode.present("pane", now=100000) is bool(fires)
    assert "timer_id" not in operator_mode._load("pane")
    assert InboxStore(clocked_scheduler.state.redis).inbox("pane") == []


def test_operator_off_without_a_timer_keeps_the_session_off(clocked_scheduler):
    from scripts.inbox.store import InboxStore

    operator_mode.observe("pane", "operator off", True, now=100)
    assert not operator_mode.present("pane", now=100)
    assert operator_mode._load("pane") == {"on": False, "at": 100, "turn_at": None}
    assert clocked_scheduler.run_due(now=400) == 0
    assert InboxStore(clocked_scheduler.state.redis).inbox("pane") == []


@pytest.mark.parametrize("seconds", [60, 600])
def test_the_legacy_operator_timeout_does_not_expire_presence(clocked_scheduler, monkeypatch, seconds):
    monkeypatch.setenv("AGENTIHOOKS_OPERATOR_TIMEOUT_SEC", str(seconds))
    operator_mode.observe("pane", "operator on", True, now=100)
    assert clocked_scheduler.run_due(now=100 + seconds) == 0
    assert operator_mode.present("pane", now=100 + seconds)


@pytest.mark.parametrize(("prompt", "present"), [("continue", True), ("operator off", False)])
def test_an_already_claimed_expiry_cannot_override_a_newer_prompt(clocked_scheduler, prompt, present):
    from hooks.context.operator_expiry import expire
    from scripts.inbox.store import InboxStore

    operator_mode.observe("pane", "operator on", True, now=100)
    old_event = legacy_timer(clocked_scheduler, "pane").record
    operator_mode.observe("pane", prompt, True, now=200)
    expire(old_event, "old-delivery", clocked_scheduler.state.redis)
    assert operator_mode.present("pane", now=400) is present
    assert InboxStore(clocked_scheduler.state.redis).inbox("pane") == []


def test_legacy_expiry_and_its_retry_preserve_presence_without_a_notice(clocked_scheduler):
    from hooks.context.operator_expiry import expire
    from scripts.inbox.store import InboxStore

    operator_mode.observe("pane", "operator on", True, now=100)
    event = legacy_timer(clocked_scheduler, "pane").record
    assert clocked_scheduler.run_due(now=400) == 1
    assert operator_mode.present("pane", now=400)
    assert "timer_id" not in operator_mode._load("pane")
    expire(event, "retry", clocked_scheduler.state.redis)
    assert operator_mode.present("pane", now=401)
    assert InboxStore(clocked_scheduler.state.redis).inbox("pane") == []


def test_legacy_expiry_does_not_notify_the_bound_session(clocked_scheduler, monkeypatch):
    from scripts.inbox.store import InboxStore

    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@proof")
    operator_mode.observe("session-uuid", "operator on", True, now=100)
    legacy_timer(clocked_scheduler, "session-uuid")
    assert clocked_scheduler.run_due(now=400) == 1
    assert operator_mode.present("session-uuid", now=400)
    inbox = InboxStore(clocked_scheduler.state.redis)
    assert inbox.inbox("engineer@proof") == []
    assert inbox.inbox("session-uuid") == []
