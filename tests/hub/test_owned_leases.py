"""The claim lease renewal for agents the hub owns, which the tick reap pass leaves out of its judging."""

import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.hub import owned_leases
from scripts.swarm.store import AgentRecord, RedisStore

pytestmark = pytest.mark.unit

LEASE_MS = 600_000
SHORT_MS = 1_000


def _agent(name: str, task: str) -> AgentRecord:
    return AgentRecord(name, "ci", task, execution_id=f"exe-{name}", generation=1, hive="hive-one")


def _store(*agents: AgentRecord) -> RedisStore:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    for agent in agents:
        store.redis.hset(store.key("sw", "executions"), agent.execution_id, json.dumps(asdict(agent)))
        store.redis.hset(store.key("sw", "agents"), agent.name, json.dumps(asdict(agent)))
        store.redis.set(store.key("sw", "claim", agent.task), agent.name, px=SHORT_MS)
    return store


def _backend(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    monkeypatch.setattr(owned_leases.BusConfig, "from_environ", lambda: SimpleNamespace(backend=backend))


@pytest.mark.feature("hub-owned-claim-lease-renewal")
def test_the_agent_the_legacy_list_leaves_out_gets_a_full_claim_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy, owned = _agent("ci@a-1", "t1"), _agent("ci@a-2", "t2")
    store = _store(legacy, owned)
    _backend(monkeypatch, "nats")
    owned_leases.renew(store, "sw", [legacy], LEASE_MS)
    assert LEASE_MS - SHORT_MS < store.redis.pttl(store.key("sw", "claim", "t2")) <= LEASE_MS
    assert store.redis.pttl(store.key("sw", "claim", "t1")) <= SHORT_MS


@pytest.mark.feature("hub-owned-claim-lease-renewal")
def test_a_claim_another_agent_holds_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    owned = _agent("ci@a-2", "t2")
    store = _store(owned)
    store.redis.set(store.key("sw", "claim", "t2"), "ci@a-3", px=SHORT_MS)
    _backend(monkeypatch, "nats")
    owned_leases.renew(store, "sw", [], LEASE_MS)
    assert store.redis.get(store.key("sw", "claim", "t2")) == "ci@a-3"
    assert store.redis.pttl(store.key("sw", "claim", "t2")) <= SHORT_MS


@pytest.mark.feature("hub-owned-claim-lease-renewal")
def test_a_lapsed_claim_is_never_taken_back(monkeypatch: pytest.MonkeyPatch) -> None:
    owned = _agent("ci@a-2", "t2")
    store = _store(owned)
    store.redis.delete(store.key("sw", "claim", "t2"))
    _backend(monkeypatch, "nats")
    owned_leases.renew(store, "sw", [], LEASE_MS)
    assert store.redis.exists(store.key("sw", "claim", "t2")) == 0


@pytest.mark.feature("hub-owned-claim-lease-renewal")
def test_the_memory_backend_owns_no_agent_and_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    _backend(monkeypatch, "memory")
    owned_leases.renew(store, "sw", [], LEASE_MS)
    assert store.mock_calls == []
