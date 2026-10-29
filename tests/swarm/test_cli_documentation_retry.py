from hooks.context import documentation_session
from scripts.swarm import timer
from tests.swarm.test_cli import _tick_all


def test_every_tick_pass_dispatches_the_documentation_retry_timer(monkeypatch):
    monkeypatch.setattr(timer, "entry_point", lambda: "/installed/bin/agentihooks")
    scheduled = []
    monkeypatch.setattr(documentation_session, "schedule_retry", lambda: scheduled.append(True))
    _tick_all(monkeypatch, lambda store, slug: [], ["a", "b"])
    assert scheduled == [True]
    _tick_all(monkeypatch, lambda store, slug: [], [])
    assert scheduled == [True, True]
