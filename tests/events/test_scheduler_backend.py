from unittest.mock import Mock

import pytest

from scripts.events import scheduler_state

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("environ", "chosen"),
    [
        ({}, "redis"),
        ({"AGENTIHOOKS_STATE_BACKEND": "bus"}, "bus"),
        ({"AGENTIHOOKS_SCHEDULER_STATE_BACKEND": "bus"}, "bus"),
        ({"AGENTIHOOKS_STATE_BACKEND": "bus", "AGENTIHOOKS_SCHEDULER_STATE_BACKEND": "redis"}, "redis"),
        ({"AGENTIHOOKS_STATE_BACKEND": "bus", "AGENTIHOOKS_SCHEDULER_STATE_BACKEND": ""}, "bus"),
    ],
)
def test_the_scheduler_backend_follows_its_own_setting_before_the_shared_one(environ, chosen):
    assert scheduler_state.backend(environ) == chosen


def test_an_unknown_scheduler_backend_is_refused():
    with pytest.raises(ValueError, match="^unknown state backend 'nats'; known: bus, redis$"):
        scheduler_state.backend({"AGENTIHOOKS_SCHEDULER_STATE_BACKEND": "nats"})


def test_the_scheduler_alone_moves_onto_the_bus(monkeypatch):
    from scripts.events import factory, state

    scheduler_state._bus.cache_clear()
    monkeypatch.delenv("AGENTIHOOKS_STATE_BACKEND", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_SCHEDULER_STATE_BACKEND", "bus")
    monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
    monkeypatch.delenv("AGENTIHOOKS_BUS_SCOPED", raising=False)
    bus = Mock()
    monkeypatch.setattr(factory, "open_bus", Mock(return_value=bus))
    monkeypatch.setattr(scheduler_state, "atexit", Mock())
    try:
        assert scheduler_state.open_scheduler_state("scheduler") is bus.state.return_value
        assert state.backend() == "redis"
    finally:
        scheduler_state._bus.cache_clear()


def test_the_scheduler_stays_on_redis_while_the_shared_stores_use_the_bus(monkeypatch):
    from scripts.events.redis_state import RedisStateStore
    from scripts.swarm import store

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setenv("AGENTIHOOKS_SCHEDULER_STATE_BACKEND", "redis")
    client = Mock()
    monkeypatch.setattr(store, "redis_client", Mock(return_value=client))

    opened = scheduler_state.open_scheduler_state("scheduler")

    assert isinstance(opened, RedisStateStore)
    assert opened.redis is client


def test_the_hive_timer_state_follows_the_scheduler_backend(monkeypatch):
    from scripts.events.redis_state import RedisStateStore
    from scripts.hive import daemon

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setenv("AGENTIHOOKS_SCHEDULER_STATE_BACKEND", "redis")
    waiter = Mock()

    opened = daemon.timer_state(waiter)

    assert isinstance(opened, RedisStateStore)
    assert opened.redis is waiter
