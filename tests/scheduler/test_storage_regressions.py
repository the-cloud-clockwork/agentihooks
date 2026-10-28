import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.scheduler import service, slots
from scripts.scheduler.core import Scheduler

pytestmark = pytest.mark.unit


@pytest.fixture
def state():
    bus = memory.open_bus(BusConfig())
    yield bus.state("scheduler")
    bus.close()


def test_interleaved_deliveries_keep_their_history(state):
    assert slots.first_delivery(state, "owner", "A") is True
    assert slots.first_delivery(state, "owner", "B") is True
    assert slots.first_delivery(state, "owner", "A") is False
    assert slots.first_delivery(state, "other", "A") is True


def test_distinct_concurrent_deliveries_are_not_acknowledged_without_delivery(state, monkeypatch):
    barrier = Barrier(2)
    create = state.create

    def together(key, value):
        barrier.wait(timeout=5)
        return create(key, value)

    monkeypatch.setattr(state, "create", together)
    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(lambda delivery: slots.first_delivery(state, "owner", delivery), ["A", "B"]))
    assert results == [True, True]


def test_legacy_schedules_preserve_pending_delivery_and_retirement(state, monkeypatch):
    import fakeredis

    from scripts.swarm import store

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    monkeypatch.setattr(store, "redis_client", lambda: redis)
    old = Scheduler(state, machine="legacy")
    entry = old.timer(owner="owner", payload="payload", delay=2, now=10)
    entry.start(now=10)
    old.change(lambda document: old._claim(document, 12))
    record = entry.record
    redis.hset(old.key("entries"), entry.id, json.dumps(record))
    redis.hset(old.key("retired"), "retired", "1")
    migrated = Scheduler(machine="legacy")
    assert migrated.get(entry.id).record == record
    assert migrated.document()["retired"] == {"retired": True}
    migrated.update(entry.id, lambda current: current.__setitem__("payload", "changed"))
    assert Scheduler(machine="legacy").get(entry.id).record["payload"] == "changed"


def test_bus_only_scheduler_starts_its_supervisor(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    monkeypatch.setattr(service, "installed", lambda: True)
    spawn = Mock()
    monkeypatch.setattr(service, "subprocess", SimpleNamespace(Popen=spawn, DEVNULL=service.subprocess.DEVNULL))
    env = {"AGENTIHOOKS_STATE_BACKEND": "bus", "AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_URL": "nats://one:4222"}
    assert service.ensure_running(env) is True
    assert spawn.call_args.kwargs["env"] == env


def test_bus_runtime_locks_separate_endpoints_and_redis():
    first = {"AGENTIHOOKS_STATE_BACKEND": "bus", "AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_URL": "nats://one:4222"}
    second = {**first, "AGENTIHOOKS_BUS_URL": "nats://two:4222"}
    assert len({service.runtime_dir(first), service.runtime_dir(second), service.runtime_dir({})}) == 3
