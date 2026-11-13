"""The runtime port fences the pass runtime to the held lease."""

from typing import Any, cast

import pytest

from scripts.swarm import cli, controller, lease
from scripts.tick.adapters.fakes import FakeRuntime, fake_tools
from scripts.tick.adapters.runtime import RouterRuntime
from scripts.tick.deadline import Deadline
from scripts.tick.ports import UndeclaredEffect
from scripts.tick.steps import masters

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


@pytest.mark.feature("tick-runtime-port-fence")
def test_the_master_placement_ports_run_under_one_lease_life(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lease, "ttl_ms", lambda: 30_000)

    deadline = masters._lease_deadline()  # pyright: ignore[reportPrivateUsage]

    assert abs(deadline.remaining - 30.0) < 0.01
