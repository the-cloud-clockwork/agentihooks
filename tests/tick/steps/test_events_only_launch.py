"""Every swarm runs on the events engine: no tick step launches an agent and a new or unmarked config reads events."""

import pytest

from scripts.swarm.store import EVENTS_ENGINE, RedisStore, SwarmConfig, SwarmError
from scripts.tick import registry

pytestmark = pytest.mark.unit

LAUNCH_STEPS = frozenset({"spawn", "spawn due"})


@pytest.mark.feature("swarm-events-only-launch")
def test_no_registered_tick_step_launches_an_agent() -> None:
    names = {step.name for step in registry.steps()}
    assert sorted(names & LAUNCH_STEPS) == []


@pytest.mark.feature("swarm-events-only-launch")
def test_a_config_built_without_an_engine_runs_on_events() -> None:
    assert SwarmConfig("sw", "/repo", 1, 1).engine == EVENTS_ENGINE


@pytest.mark.feature("swarm-events-only-launch")
def test_a_stored_config_without_an_engine_field_reads_as_events() -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    store.redis.hdel(store.key("sw", "config"), "engine")
    assert store.config("sw").engine == EVENTS_ENGINE


@pytest.mark.feature("swarm-events-only-launch")
def test_a_config_naming_the_legacy_engine_is_refused() -> None:
    with pytest.raises(SwarmError) as refused:
        SwarmConfig("sw", "/repo", 1, 1, engine="legacy")
    assert str(refused.value) == "engine must be events"


@pytest.mark.feature("swarm-events-only-launch")
def test_a_stored_legacy_engine_reads_as_events() -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    store.redis.hset(store.key("sw", "config"), "engine", "legacy")
    assert store.config("sw").engine == EVENTS_ENGINE
