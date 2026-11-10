"""The runtime port fences the pass runtime to the held lease, and the ack pass takes its lease and fence through the
port tools."""

import os
from typing import Any, cast

import pytest

from scripts.swarm import ack_pass, cli, commands, controller, lease, ledger_client
from scripts.swarm import tick as engine
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.tick.adapters.fakes import FakeCoordination, FakeRuntime, fake_tools
from scripts.tick.adapters.runtime import RouterRuntime
from scripts.tick.deadline import Deadline
from scripts.tick.ports import UndeclaredEffect

pytestmark = pytest.mark.unit

EXPIRED = Deadline(0, lambda: 0)


class _Legacy:
    """A pass runtime that names its live agents."""

    def __init__(self, name: str) -> None:
        """Name the runtime ``name``."""
        self.name = name

    def live_names(self) -> set[str]:
        """Return the one live agent this runtime names after itself."""
        return {f"agent of {self.name}"}


def _router(fencing: Any = None) -> RouterRuntime:
    """Return a router runtime over the raw pass runtime with the fence ``fencing``."""
    return RouterRuntime(cast(Any, None), lambda name: None, legacy=_Legacy("raw"), fencing=fencing)


@pytest.mark.feature("tick-runtime-port-fence")
def test_the_runtime_port_fences_its_pass_runtime_and_acts_through_the_fenced_one_after() -> None:
    fenced: list[tuple[object, str, str]] = []

    def fence(held: object, deployment: str, runtime: Any) -> _Legacy:
        fenced.append((held, deployment, runtime.name))
        return _Legacy("fenced")

    adapter = _router(fence)
    deadline = Deadline(10, lambda: 0)

    assert adapter.live_names(deadline=deadline) == {"agent of raw"}
    returned = adapter.fence("held", "local", deadline=deadline)

    assert returned is adapter.legacy
    assert returned.name == "fenced"
    assert fenced == [("held", "local", "raw")]
    assert adapter.live_names(deadline=deadline) == {"agent of fenced"}


@pytest.mark.feature("tick-runtime-port-fence")
def test_a_runtime_port_without_a_fence_refuses_to_fence() -> None:
    adapter = _router()

    with pytest.raises(RuntimeError) as refused:
        adapter.fence("held", "local", deadline=Deadline(10, lambda: 0))

    assert str(refused.value) == "this runtime port has no fence"
    assert adapter.legacy.name == "raw"


@pytest.mark.feature("tick-runtime-port-fence")
def test_the_runtime_port_refuses_to_fence_past_its_deadline() -> None:
    adapter = _router(lambda held, deployment, runtime: pytest.fail("fenced past the deadline"))

    with pytest.raises(TimeoutError) as late:
        adapter.fence("held", "local", deadline=EXPIRED)

    assert str(late.value) == "tool deadline expired"
    assert adapter.legacy.name == "raw"


@pytest.mark.feature("tick-runtime-port-fence")
def test_fencing_is_a_runtime_write_a_step_must_declare() -> None:
    tools = fake_tools()

    with pytest.raises(UndeclaredEffect) as error:
        tools.for_effects(frozenset()).runtime.fence("held", "local", deadline=Deadline.after(10))
    tools.for_effects(frozenset({"runtime"})).runtime.fence("held", "local", deadline=Deadline.after(10))

    assert str(error.value) == "runtime is not a declared effect"
    assert [(c.operation, c.args) for c in cast(FakeRuntime, tools.runtime).calls] == [("fence", ("held", "local"))]


@pytest.mark.feature("tick-runtime-port-fence")
def test_the_pass_tools_fence_builds_the_controller_fence_of_their_swarm(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[tuple[Any, ...]] = []
    monkeypatch.setattr(controller, "FencedRuntime", lambda *args: made.append(args) or "fenced")

    fencing = cli._fencing  # pyright: ignore[reportPrivateUsage]

    assert fencing(cast(Any, "store"), "sw")("held", "hybrid", "raw") == "fenced"

    assert made == [("store", "sw", "held", "raw", "hybrid")]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    """Return a swarm store holding swarm ``sw`` with one launch waiting for its acknowledgement."""
    import fakeredis

    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    monkeypatch.delenv("LEDGER_URL", raising=False)
    monkeypatch.delenv("SWARM_URL", raising=False)
    made = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    made.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    made.record_launch("sw", AgentRecord("engineer@a1b2c3-0001", "eng", "t1", started_at=1_000), "acknowledging")
    return made


@pytest.mark.feature("tick-runtime-port-fence")
def test_the_ack_pass_takes_its_lease_and_fence_through_the_port_tools(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = lease.acquire(store, "sw", commands.hive_id())
    assert held is not None
    tools = fake_tools()
    cast(FakeCoordination, tools.coordination).answers["lease"] = held
    port = cast(FakeRuntime, tools.runtime)
    port.answers["fence"] = "fenced"
    built: list[tuple[Any, ...]] = []
    collected: list[Any] = []
    monkeypatch.setattr(cli, "_pass_tools", lambda *args: built.append(args) or tools)
    monkeypatch.setattr(controller, "FencedRuntime", lambda *args: pytest.fail("fenced outside the runtime port"))
    monkeypatch.setattr(ledger_client, "pass_state", lambda ledger, slug: {"tasks": []})
    monkeypatch.setattr(engine, "_collect_acks", lambda *args: collected.append(args[4]) or ["collected"])

    assert ack_pass.run(store, "sw", ledger="ledger", runtime="runtime") == ["collected"]

    assert built == [(store, "sw", commands.hive_id(), "ledger", "runtime")]
    assert [c.operation for c in cast(FakeCoordination, tools.coordination).calls] == ["lease"]
    deployment = controller.deployment_for(store.config("sw"), os.environ)
    assert [(c.operation, c.args) for c in port.calls] == [("fence", (held, deployment))]
    assert collected == ["fenced"]


@pytest.mark.feature("tick-runtime-port-fence")
def test_the_ack_pass_collects_nothing_when_the_coordination_port_holds_no_lease(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = fake_tools()
    monkeypatch.setattr(cli, "_pass_tools", lambda *args: tools)
    monkeypatch.setattr(engine, "_collect_acks", lambda *args: pytest.fail("collected without a lease"))

    assert ack_pass.run(store, "sw", ledger="ledger", runtime="runtime") == []

    assert cast(FakeRuntime, tools.runtime).calls == []
