"""The hive drain notice tick step tells the master of each swarm on a draining hive of that drain, once, from the tick."""

from dataclasses import replace
from importlib import import_module
from typing import Any

import pytest

from scripts.hive import master_notice, registry
from scripts.inbox.store import InboxStore, Item
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.tick import registry as tick_registry
from scripts.tick.steps import CallStep, budgets, host

pytestmark = pytest.mark.unit

NOW_MS = 1_800_000_000_000
HIVE = "proof"
SWARMS = ("sw", "other", "idle")
PLACED = (("sw", "eng", "working"), ("sw", "ci", "working"), ("other", "eng", "working"), ("idle", "eng", "finished"))
STOPPING = registry.Drain("worker", "the hive is stopping")
MEMBER = {
    "AGENTIHOOKS_DEPLOYMENT": "cluster",
    "AGENTIHOOKS_HIVE_ID": HIVE,
    "AGENTIHOOKS_HIVE_REDIS_URL": "redis://hive.example:6380/2",
}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    """Three swarms on one fake Redis, with a registry clock that moves one millisecond per read."""
    import fakeredis

    ticks = iter(range(NOW_MS, NOW_MS + 1_000_000))
    monkeypatch.setattr(registry, "wall_ms", lambda: next(ticks))
    result = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    for slug in SWARMS:
        result.create(SwarmConfig(slug, "/repo", 1, 1))
    return result


def _agents(store: RedisStore) -> dict[str, list[str]]:
    """Two live agents of sw, one of other and a finished one of idle, all started on the hive; names per swarm."""
    names: dict[str, list[str]] = {}
    for slug, lane, state in PLACED:
        record = AgentRecord(store.next_name(slug, lane), lane, f"t-{lane}", seat=f"{lane}-1@{slug}", hive=HIVE)
        agent = store.start_execution(slug, replace(record, state="working"))
        if state != "working":
            store.put_agent(slug, replace(agent, state=state))
        names.setdefault(slug, []).append(agent.name)
    return {slug: sorted(found) for slug, found in names.items()}


def _master(store: RedisStore, slug: str) -> list[Item]:
    return InboxStore(store.redis).inbox(f"master@{slug}")


def _texts(store: RedisStore, slug: str) -> list[str]:
    return [item.text for item in _master(store, slug)]


def _drained(names: list[str]) -> str:
    return f"hive {HIVE} is draining: the hive is stopping. Agents of this swarm on it: {', '.join(names)}"


def _step() -> CallStep:
    import_module("scripts.tick.steps.host")
    found = {step.name: step for step in tick_registry.steps()}["hive drain notice"]
    assert isinstance(found, CallStep)
    return found


def _pass(store: RedisStore) -> Any:
    """Run one pass of the registered step with the swarm store, failing on any exception the step held."""
    values: dict[str, Any] = {"box": {}, "store": store}
    actions = _step().call(import_module("scripts.swarm.tick"), values.__getitem__)
    assert values["box"] == {}
    return actions


def _member_stop(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """A worker pod stop drain written and announced as the hive member, which reaches no master, then the member
    environment is gone again as it is for the tick controller."""
    registry.update(store.redis, HIVE, ["ui=no"])
    names = _agents(store)
    record = registry.update(store.redis, HIVE, ["draining=yes"], drain=STOPPING)
    for name, value in MEMBER.items():
        monkeypatch.setenv(name, value)
    master_notice.drained(store, HIVE, record)
    for name in MEMBER:
        monkeypatch.delenv(name)
    assert [_texts(store, slug) for slug in SWARMS] == [[], [], []]
    return names


@pytest.mark.feature("hive-stop-drain-notice")
def test_hive_drain_notice() -> None:
    """The hive drain notice step registers in group wrapper health on the slow lane every two minutes with its
    feature and ports, and keeps running past a ledger stop."""
    found = _step()
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper health",
        "slow",
        "hive-stop-drain-notice",
        frozenset({"coordination"}),
    )
    assert found.interval_s == budgets.WIDE_S
    assert "hive drain notice" in host.REDIS_ONLY


@pytest.mark.feature("hive-stop-drain-notice")
def test_a_worker_member_stop_drain_reaches_each_served_master_once_from_the_tick(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = _member_stop(store, monkeypatch)

    assert _pass(store) == []

    assert [_texts(store, slug) for slug in SWARMS] == [[_drained(names["sw"])], [_drained(names["other"])], []]
    sent = [item for slug in SWARMS for item in _master(store, slug)]
    assert [(item.sender, item.fyi) for item in sent] == [("swarm", True), ("swarm", True)]


@pytest.mark.feature("hive-stop-drain-notice")
def test_a_second_pass_over_the_same_drain_sends_no_new_master_item(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = _member_stop(store, monkeypatch)
    _pass(store)

    _pass(store)

    assert [_texts(store, slug) for slug in SWARMS] == [[_drained(names["sw"])], [_drained(names["other"])], []]


@pytest.mark.feature("hive-stop-drain-notice")
def test_a_hive_whose_newest_drain_entry_no_longer_reads_draining_tells_no_master(store: RedisStore) -> None:
    registry.update(store.redis, HIVE, ["ui=no"])
    _agents(store)
    registry.update(store.redis, HIVE, ["draining=yes"], drain=STOPPING)
    registry.update(store.redis, HIVE, ["draining=no"], drain=registry.Drain("operator", "the release is done"))

    _pass(store)

    assert [_texts(store, slug) for slug in SWARMS] == [[], [], []]
