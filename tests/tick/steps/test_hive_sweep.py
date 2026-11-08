"""The hive sweep tick step drains and removes quiet ephemeral hives, and quiet lasting hives raise one finding."""

from dataclasses import replace
from importlib import import_module

import pytest

from scripts.hive import hive_state, purge, registry, sweep
from scripts.swarm.health import quiet_hives
from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.tick import registry as tick_registry

NOW_MS = 1_800_000_000_000
QUIET_AT = NOW_MS - registry.QUIET_MS - 1


@pytest.fixture
def store() -> RedisStore:
    """A swarm store on a fake Redis holding one swarm."""
    import fakeredis

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("sw", "/repo", 1, 1))
    return result


def _hive(store: RedisStore, hive: str, ephemeral: str, beat: int, ui: str = "no") -> None:
    registry.update(store.redis, hive, [f"ephemeral={ephemeral}", f"ui={ui}"])
    registry.beat(store.redis, hive, {"heartbeat_at": beat})
    store.redis.set(f"{registry.key(hive)}:sessions", "{}")


@pytest.mark.feature("hive-quiet-sweep")
def test_hive_sweep() -> None:
    """The hive sweep step registers in group wrapper health on the slow lane with its feature and ports."""
    import_module("scripts.tick.steps.host")
    found = {step.name: step for step in tick_registry.steps()}["hive sweep"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper health",
        "slow",
        "hive-quiet-sweep",
        frozenset({"coordination"}),
    )


@pytest.mark.feature("hive-quiet-sweep")
def test_a_quiet_ephemeral_hive_is_drained_and_removed_after_the_window(store: RedisStore) -> None:
    """Only the ephemeral hive quiet past the window loses its records; fresh, lasting and interface hives stay."""
    _hive(store, "proof", "yes", QUIET_AT)
    _hive(store, "fresh", "yes", NOW_MS - registry.QUIET_MS)
    _hive(store, "lasting", "no", QUIET_AT)
    _hive(store, "desk", "yes", QUIET_AT, ui="yes")
    lines = sweep.sweep(store, NOW_MS)
    assert lines[0] == "drained quiet ephemeral hive proof"
    assert lines[1].startswith("deleted ") and lines[1].endswith(" registry records of proof")
    assert registry.show(store.redis, "proof") is None
    assert not store.redis.exists(f"{registry.key('proof')}:sessions")
    assert sorted(hive["id"] for hive in registry.hives(store.redis)) == ["desk", "fresh", "lasting"]
    assert sweep.sweep(store, NOW_MS) == []


@pytest.mark.feature("hive-quiet-sweep")
def test_a_quiet_ephemeral_hive_with_a_running_agent_record_is_drained_and_kept(store: RedisStore) -> None:
    """The sweep marks the hive draining with its owner and reason, then removes it once its agents end."""
    _hive(store, "proof", "yes", QUIET_AT)
    agent = AgentRecord(store.next_name("sw", "eng"), "eng", "t1", seat="eng-1@sw", hive="proof", state="working")
    store.put_agent("sw", agent)
    assert sweep.sweep(store, NOW_MS) == [
        "drained quiet ephemeral hive proof",
        "kept quiet hive proof while 1 agent records still run on it",
    ]
    held = registry.show(store.redis, "proof")
    assert held is not None and held["draining"] == "yes"
    assert (held["drains"][-1]["owner"], held["drains"][-1]["reason"]) == (sweep.OWNER, sweep.REASON)
    store.put_agent("sw", replace(agent, state="ended"))
    assert sweep.sweep(store, NOW_MS)[-1].endswith(" registry records of proof")
    assert registry.show(store.redis, "proof") is None


@pytest.mark.feature("hive-quiet-sweep")
def test_a_quiet_lasting_hive_raises_one_finding_and_an_ephemeral_one_none(store: RedisStore) -> None:
    """Each non ephemeral hive past the window is one quiet hive finding naming its silence in minutes."""
    _hive(store, "lasting", "no", QUIET_AT)
    _hive(store, "proof", "yes", QUIET_AT)
    _hive(store, "fresh", "no", NOW_MS - registry.QUIET_MS)
    found = quiet_hives.findings(store.redis, NOW_MS)
    assert [(f.id, f.measure, f.threshold) for f in found] == [("quiet-hive/lasting", 60, quiet_hives.THRESHOLD)]
    assert found[0].summary == "Hive lasting has sent no heartbeat for 60 minutes"


@pytest.mark.feature("hive-quiet-sweep")
def test_purge_deletes_every_record_of_a_hive_and_keeps_its_wait_histories(store: RedisStore) -> None:
    """Purge counts the hash, the keys under it and the state record, unindexes the hive and moves wait histories."""
    _hive(store, "proof", "yes", QUIET_AT)
    source = f"{registry.key('proof')}:swarm:sw:wait-history:engineer@1"
    store.redis.hset(source, mapping={"1": "proof"})
    store.redis.set(f"{registry.key('other')}:sessions", "{}")
    assert purge.keys(store.redis, "proof") == [
        registry.key("proof"),
        *store.redis.scan_iter(f"{registry.key('proof')}:*"),
    ]
    assert purge.recorded(store.redis, "proof") is True
    held = sum(store.redis.exists(key) for key in purge.keys(store.redis, "proof"))
    assert held >= 2
    assert purge.forget(store.redis, "proof") == [f"deleted {held + 1} registry records of proof"]
    assert store.redis.hgetall(f"{ROOT}:swarm:sw:wait-history:engineer@1") == {"1": "proof"}
    assert purge.recorded(store.redis, "proof") is False
    assert "proof" not in hive_state.index(store.redis)
    assert store.redis.exists(f"{registry.key('other')}:sessions") == 1


@pytest.mark.feature("hive-quiet-sweep")
def test_purge_counts_only_agents_still_running_on_the_hive(store: RedisStore) -> None:
    """Hosted lists the hive's working agents and leaves finished, ended and elsewhere agents out."""
    for n, (hive, state) in enumerate(
        (("proof", "working"), ("proof", "finished"), ("proof", "ended"), ("x", "working"))
    ):
        store.put_agent(
            "sw", AgentRecord(f"engineer@{n}", "eng", f"t{n}", seat=f"eng-{n + 1}@sw", hive=hive, state=state)
        )
    assert [(slug, agent.name) for slug, agent in purge.hosted(store, "proof")] == [("sw", "engineer@0")]


@pytest.mark.feature("hive-quiet-sweep")
@pytest.mark.parametrize(("beat", "expected"), [(0, False), (NOW_MS - registry.QUIET_MS, False), (QUIET_AT, True)])
def test_a_hive_is_quiet_only_once_a_beat_is_older_than_the_window(beat: int, expected: bool) -> None:
    """A hive that never beat is never quiet; one beaten exactly at the window edge is not yet quiet."""
    assert registry.quiet({"heartbeat_at": beat}, NOW_MS) is expected
