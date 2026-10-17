from unittest.mock import Mock

import pytest

from hooks.context import operator_mode

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def clocked_scheduler(monkeypatch):
    import fakeredis

    from scripts.scheduler import Scheduler

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("hooks._redis.get_redis", lambda: redis)
    monkeypatch.setattr("scripts.scheduler.service.ensure_running", Mock(return_value=True))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof")
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
    assert operator_mode.present("other", now=400)
    notices = InboxStore(clocked_scheduler.redis).inbox("pane")
    assert [item.text for item in notices] == [
        "Operator off: no typed prompt arrived before the operator timer expired."
    ]
    assert InboxStore(clocked_scheduler.redis).inbox("other") == []
    assert restarted.run_due(now=401) == 0
    assert len(InboxStore(clocked_scheduler.redis).inbox("pane")) == 1
