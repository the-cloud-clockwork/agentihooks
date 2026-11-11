"""The master placement takes the controller tick lock and drops gone masters only through the coordination port."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.swarm import controller, lease, tick
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.tick.adapters.coordination import StoreCoordination
from scripts.tick.adapters.fakes import FakeCoordination, FakeRuntime, fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.ports import UndeclaredEffect
from scripts.tick.steps import masters

pytestmark = pytest.mark.unit

SEAT = "master@sw"


def _deadline() -> Deadline:
    """Return a deadline that holds for the whole test."""
    return Deadline(60, lambda: 0)


@pytest.fixture
def store() -> RedisStore:
    """Return a swarm store over fake Redis holding the running swarm ``sw``."""
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", "/repo", 2, 1))
    return saved


class _NullPass:
    """A held pass that does nothing on entry and exit."""

    def __enter__(self) -> None:
        """Enter the pass."""

    def __exit__(self, *exc: object) -> None:
        """Leave the pass."""


def _null_pass(store: object, slug: str, held: object) -> _NullPass:
    """Return a held pass that does nothing."""
    return _NullPass()


def _locking(monkeypatch: pytest.MonkeyPatch, token: object) -> tuple[Any, FakeCoordination]:
    """Refuse the tick lock off the port; return a running swarm store and a coordination port holding a lease
    that answers ``token`` for the tick lock."""
    monkeypatch.setattr(controller, "take_tick_lock", lambda *args: pytest.fail("tick lock taken off the port"))
    monkeypatch.setattr(controller, "release_tick_lock", lambda *args: pytest.fail("tick lock released off the port"))
    monkeypatch.setattr(lease, "ttl_ms", lambda: 30)
    monkeypatch.setattr(lease, "held_pass", _null_pass)
    running = SimpleNamespace(state="running")
    store = SimpleNamespace(config=lambda slug: running, ensure_code=lambda slug: running)
    return store, FakeCoordination(answers={"lease": "held", "tick_lock": token})


@pytest.mark.feature("tick-master-placement-coordination")
def test_the_store_coordination_takes_refuses_and_releases_the_tick_lock(store: RedisStore) -> None:
    port = StoreCoordination(store, "sw", "hive")
    held = port.lease(deadline=_deadline())
    token = port.tick_lock(held, 30_000, "place master", deadline=_deadline())
    assert token is not None
    assert store.redis.get(store.key("sw", "tick-lock")) == token
    assert port.tick_lock(held, 30_000, "place master", deadline=_deadline()) is None
    port.release_tick_lock(token, "place master", deadline=_deadline())
    assert store.redis.get(store.key("sw", "tick-lock")) is None


@pytest.mark.feature("tick-master-placement-coordination")
def test_the_tick_lock_is_a_coordination_write_a_step_must_declare() -> None:
    tools = fake_tools()
    undeclared = tools.for_effects(frozenset()).coordination
    with pytest.raises(UndeclaredEffect):
        undeclared.tick_lock("held", 30, "caller", deadline=_deadline())
    with pytest.raises(UndeclaredEffect):
        undeclared.release_tick_lock("token", "caller", deadline=_deadline())
    declared = tools.for_effects(frozenset({"coordination"})).coordination
    declared.tick_lock("held", 30, "caller", deadline=_deadline())
    declared.release_tick_lock("token", "caller", deadline=_deadline())
    assert [(c.operation, c.args) for c in cast(FakeCoordination, tools.coordination).calls] == [
        ("tick_lock", ("held", 30, "caller")),
        ("release_tick_lock", ("token", "caller")),
    ]


@pytest.mark.feature("tick-master-placement-coordination")
def test_the_master_placement_takes_and_releases_the_tick_lock_through_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, port = _locking(monkeypatch, "token")
    acted = masters._under_tick_lock("sw", store, lambda config, held: [f"acted {held}"], "place master", port)
    assert acted == ["acted held"]
    assert [(c.operation, c.args) for c in port.calls] == [
        ("lease", ()),
        ("tick_lock", ("held", 30, "place master")),
        ("release_tick_lock", ("token", "place master")),
    ]


@pytest.mark.feature("tick-master-placement-coordination")
def test_the_master_placement_waits_when_the_port_refuses_the_tick_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    store, port = _locking(monkeypatch, None)
    acted = masters._under_tick_lock("sw", store, lambda config, held: pytest.fail("acted unlocked"), "caller", port)
    assert acted == []
    assert [c.operation for c in port.calls] == ["lease", "tick_lock"]


@pytest.mark.feature("tick-master-placement-coordination")
def test_respawn_master_drops_a_gone_master_through_the_coordination_port(monkeypatch: pytest.MonkeyPatch) -> None:
    gone = AgentRecord("master@abc123-0001", MASTER, MASTER, seat=SEAT, state="working")
    store = SimpleNamespace(agents=lambda slug: [gone], drop_agent=lambda *args: pytest.fail("dropped off the port"))
    tools = fake_tools()
    cast(FakeRuntime, tools.runtime).answers["fence"] = SimpleNamespace(live_names=lambda: set())
    monkeypatch.setattr(masters, "_master_tools", lambda slug, store, runtime: tools)
    monkeypatch.setattr(tick, "_under_tick_lock", lambda slug, store, act, caller, port: act("config", "held"))
    monkeypatch.setattr(tick, "_recover_master", lambda *args: [])
    monkeypatch.setattr(tick, "_master", lambda *args: ["spawned"])
    assert masters.respawn_master("sw", store, "runtime", 1_000) == [f"dropped the gone master {gone.name}", "spawned"]
    writes = [c.args for c in cast(FakeCoordination, tools.coordination).calls if c.operation == "store_write"]
    assert writes == [("drop_agent", ("sw", gone.name), {})]
