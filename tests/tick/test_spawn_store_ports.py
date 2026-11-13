"""The coordination operations the spawn step reaches the swarm store through: its facade, the store adapter, the fake and the step."""

from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from typing import Any, Iterator, cast

import pytest

from scripts.swarm import timing
from scripts.swarm.store import PREFIX, AgentRecord
from scripts.tick.adapters.calls import SpawnStoreCalls
from scripts.tick.adapters.coordination import StoreCoordination
from scripts.tick.adapters.fakes import FakeCoordination, FakeInbox, FakeLedger, fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.ports import UndeclaredEffect
from scripts.tick.step import Context, Domain
from scripts.tick.steps import PASS, common, place

pytestmark = pytest.mark.unit

DEADLINE = Deadline(10, lambda: 0)
EXPIRED = Deadline(0, lambda: 0)


def _calls(answers: dict[str, object] | None = None) -> tuple[FakeCoordination, SpawnStoreCalls]:
    port = FakeCoordination(answers=answers or {})
    return port, SpawnStoreCalls(port, DEADLINE, FakeInbox())


def _seen(port: FakeCoordination) -> list[tuple[str, tuple[object, ...], Deadline]]:
    return [(call.operation, call.args, call.deadline) for call in port.calls]


@pytest.mark.feature("swarm-tick-spawn")
def test_the_spawn_store_writes_through_the_coordination_port() -> None:
    port, store = _calls({"store:claim": True})

    assert store.claim("sw", "t1", "eng-1", 5) is True
    assert store.record_launch("sw", "record", "started", after="t0") is None

    assert _seen(port) == [
        ("store_write", ("claim", ("sw", "t1", "eng-1", 5), {}), DEADLINE),
        ("store_write", ("record_launch", ("sw", "record", "started"), {"after": "t0"}), DEADLINE),
    ]


@pytest.mark.feature("swarm-tick-spawn")
def test_the_spawn_store_reads_through_the_coordination_port() -> None:
    port, store = _calls({"store:agents": ["eng-1"], "store:handoff_envelope": {"reason": "quota"}})

    assert store.agents("sw") == ["eng-1"]
    assert store.handoff_envelope("sw", "t1") == {"reason": "quota"}

    assert _seen(port) == [
        ("store_read", ("agents", ("sw",), {}), DEADLINE),
        ("store_read", ("handoff_envelope", ("sw", "t1"), {}), DEADLINE),
    ]


@pytest.mark.feature("swarm-tick-spawn")
def test_the_spawn_store_reaches_redis_seats_and_memory_through_the_port() -> None:
    port, store = _calls({"store:redis.hget": "saved"})

    assert store.redis.hget("k", "t1") == "saved"
    store.redis.zadd("spends", {"eng-1": 7})
    store.seats.occupy("eng-1@sw", "eng-1", 7)
    store.memory.recaps("eng-1@sw")

    assert _seen(port) == [
        ("store_read", ("redis.hget", ("k", "t1"), {}), DEADLINE),
        ("store_write", ("redis.zadd", ("spends", {"eng-1": 7}), {}), DEADLINE),
        ("store_write", ("seats.occupy", ("eng-1@sw", "eng-1", 7), {}), DEADLINE),
        ("store_read", ("memory.recaps", ("eng-1@sw",), {}), DEADLINE),
    ]


@pytest.mark.feature("swarm-tick-spawn")
def test_the_spawn_store_builds_keys_without_a_port_call() -> None:
    port, store = _calls()

    assert store.key("sw", "launch-assignments") == f"{PREFIX}:sw:launch-assignments"
    assert port.calls == []


@pytest.mark.feature("swarm-tick-spawn")
def test_the_spawn_store_keeps_the_store_method_names_for_step_timing() -> None:
    _, store = _calls()

    assert timing.named(store.claim) == "scripts.swarm.store.RedisStore.claim"
    assert timing.named(store.redis.hget) == "scripts.swarm.store.RedisStore.redis.hget"


@pytest.mark.feature("swarm-tick-spawn")
@pytest.mark.parametrize(
    ("reach", "name"),
    [(lambda store: store._untyped, "_untyped"), (lambda store: store.redis._client, "redis._client")],
)
def test_the_spawn_store_refuses_private_names(reach: Any, name: str) -> None:
    port, store = _calls()

    with pytest.raises(AttributeError) as raised:
        reach(store)
    assert str(raised.value) == name
    assert port.calls == []


class _Raw:
    def __init__(self) -> None:
        self.redis = SimpleNamespace(hget=lambda *args, **kwargs: ("hget", args, kwargs))

    def claim(self, *args: object, **kwargs: object) -> tuple[str, tuple[object, ...], dict[str, object]]:
        return "claim", args, kwargs

    def _untyped(self) -> "_Raw":
        return self


def _adapter() -> StoreCoordination:
    return StoreCoordination(cast(Any, _Raw()), "sw", "home")


@pytest.mark.feature("swarm-tick-spawn")
def test_the_store_coordination_runs_the_named_store_operation() -> None:
    adapter = _adapter()

    assert adapter.store_write("claim", ("sw", "t1"), {"lease": 5}, deadline=DEADLINE) == (
        "claim",
        ("sw", "t1"),
        {"lease": 5},
    )
    assert adapter.store_read("redis.hget", ("k",), {"field": "t1"}, deadline=DEADLINE) == (
        "hget",
        ("k",),
        {"field": "t1"},
    )


@pytest.mark.feature("swarm-tick-spawn")
@pytest.mark.parametrize("operation", ["_untyped", "redis.__class__"])
def test_the_store_coordination_refuses_private_operations(operation: str) -> None:
    with pytest.raises(AttributeError) as raised:
        _adapter().store_write(operation, (), {}, deadline=DEADLINE)
    assert str(raised.value) == operation.rsplit(".", 1)[-1]


@pytest.mark.feature("swarm-tick-spawn")
@pytest.mark.parametrize("operation", ["claim", "seats.occupy", "redis.set"])
def test_the_store_coordination_refuses_a_write_named_as_a_read(operation: str) -> None:
    with pytest.raises(UndeclaredEffect) as raised:
        _adapter().store_read(operation, (), {}, deadline=DEADLINE)
    assert str(raised.value) == f"{operation} is a store write"


@pytest.mark.feature("swarm-tick-spawn")
@pytest.mark.parametrize("read", [True, False])
def test_the_store_coordination_refuses_operations_past_their_deadline(read: bool) -> None:
    adapter = _adapter()
    operation = adapter.store_read if read else adapter.store_write

    with pytest.raises(TimeoutError) as raised:
        operation("claim", (), {}, deadline=EXPIRED)
    assert str(raised.value) == "tool deadline expired"


@pytest.mark.feature("swarm-tick-spawn")
def test_spawn_store_writes_need_the_coordination_effect() -> None:
    tools = fake_tools(frozenset())
    store = SpawnStoreCalls(tools.coordination, DEADLINE, tools.inbox)

    assert store.agents("sw") is None
    with pytest.raises(UndeclaredEffect) as raised:
        store.claim("sw", "t1", "eng-1", 5)
    assert str(raised.value) == "coordination is not a declared effect"


@pytest.mark.feature("swarm-tick-spawn")
def test_a_dropped_spawn_claim_releases_through_coordination_and_settles_through_the_inbox() -> None:
    port, inbox = FakeCoordination(), FakeInbox()
    store = SpawnStoreCalls(port, DEADLINE, inbox)
    agent = AgentRecord("eng-1", "eng", "t1", seat="eng-1@sw")

    assert common._drop("sw", store, FakeLedger(), {}, agent) == ""

    assert _seen(port) == [
        ("store_read", ("claim_generation", ("sw", "t1"), {}), DEADLINE),
        ("store_write", ("release", ("sw", "t1", "eng-1"), {}), DEADLINE),
        ("store_write", ("drop_agent", ("sw", "eng-1"), {}), DEADLINE),
    ]
    assert [(call.operation, call.args, call.deadline) for call in inbox.calls] == [
        ("settle_exit", ("eng-1", "", "stopped"), DEADLINE)
    ]


class _Untouched:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the spawn step reached the raw store for {name}")


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


def _spawn(slug: str, config: Any, store: Any, ledger: Any, runtime: Any, *rest: Any) -> list[str]:
    store.agents(slug)
    store.redis.hget(store.key(slug, "launch-assignments"), "t1")
    store.put_agent(slug, "record")
    runtime.has_capacity(config)
    return ["spawned"]


@contextmanager
def _placement_pass(runtime: Any) -> Iterator[None]:
    yield


def _tick() -> SimpleNamespace:
    return SimpleNamespace(
        readiness=SimpleNamespace(tick_launches=lambda engine_name: True),
        panic=SimpleNamespace(state=lambda store: store.redis.get(store.key("sw", "panic"))),
        PLACING=nullcontext(),
        skip_refused=lambda function, *args: function(*args),
        _placement_pass=_placement_pass,
        _spawn=_spawn,
        SPAWN_BUDGET_S=60,
    )


@pytest.mark.feature("swarm-tick-spawn")
def test_the_spawn_step_reaches_the_store_and_runtime_only_through_its_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scripts.tick.steps.import_module", lambda name: _tick())
    tools = fake_tools()
    coordination, runtime = cast(FakeCoordination, tools.coordination), cast(Any, tools.runtime)
    state = {
        "slug": "sw",
        "config": SimpleNamespace(state="running", engine="tick"),
        "store": _Untouched(),
        "ledger": _Untouched(),
        "runtime": _Untouched(),
        "rows": {"t1": {"id": "t1", "state": "open"}},
        "doc": {},
        "now_ms": 7,
        "due": "due key",
        "box": {},
    }
    spawn = next(step for step in place.STEPS if step.name == "spawn")

    spawn.run(Context(_State(state), Deadline(50.0, lambda: 0.0), tools.for_effects(spawn.effects)))

    assert state["box"] == {"rows": state["rows"]}
    assigned = f"{PREFIX}:sw:launch-assignments"
    assert [call.args[:2] for call in coordination.calls] == [
        ("redis.get", (f"{PREFIX}:sw:panic",)),
        ("agents", ("sw",)),
        ("redis.hget", (assigned, "t1")),
        ("put_agent", ("sw", "record")),
        ("redis.delete", ("due key",)),
    ]
    assert [call.operation for call in coordination.calls] == [
        "store_read",
        "store_read",
        "store_read",
        "store_write",
        "store_write",
    ]
    assert [call.operation for call in runtime.calls] == ["has_capacity"]
    assert cast(Any, tools.inbox).calls == []
