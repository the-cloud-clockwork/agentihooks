"""FencedRuntime lists the live agent names once per tick pass and again after its own agent changes."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.hive import admission
from scripts.swarm import commands, controller, lease, local_slots
from scripts.tick.adapters.processes import PassProcesses
from scripts.tick.ports.tools import PASS_TOOLS


class Listing:
    def __init__(self) -> None:
        self.live = {"engineer@a1b2c3-0001"}
        self.listings = 0
        self.changes: list[tuple[str, tuple[Any, ...]]] = []

    def live_names(self) -> set[str]:
        self.listings += 1
        return set(self.live)

    def retire(self, agent: str, homes: tuple[str, ...] = ()) -> bool:
        self.changes.append(("retire", (agent, homes)))
        return True

    def reap_name(self, name: str) -> bool:
        self.changes.append(("reap_name", (name,)))
        return True

    def close_space(self, config: str) -> bool:
        self.changes.append(("close_space", (config,)))
        return True

    def close_pane(self, pane_id: str) -> None:
        self.changes.append(("close_pane", (pane_id,)))

    def resume(self, agent: str) -> str:
        self.changes.append(("resume", (agent,)))
        return "resumed"


def fenced(runtime: Listing) -> controller.FencedRuntime:
    store = SimpleNamespace(redis="fixture-redis")
    return controller.FencedRuntime(store, "sw", SimpleNamespace(epoch=1), runtime, controller.LOCAL)  # pyright: ignore[reportArgumentType]


def pass_tools() -> Any:
    return SimpleNamespace(processes=PassProcesses())


@pytest.fixture
def in_pass() -> Any:
    token = PASS_TOOLS.set(pass_tools())
    yield
    PASS_TOOLS.reset(token)


@pytest.mark.feature("fenced-live-names-listing")
def test_live_names_lists_once_per_pass_and_serves_copies(in_pass: Any) -> None:
    runtime = Listing()
    wrapped = fenced(runtime)
    first = wrapped.live_names()
    first.add("engineer@a1b2c3-0002")
    assert wrapped.live_names() == {"engineer@a1b2c3-0001"}
    assert runtime.listings == 1


@pytest.mark.feature("fenced-live-names-listing")
def test_live_names_lists_again_in_a_new_pass() -> None:
    runtime = Listing()
    wrapped = fenced(runtime)
    for _ in range(2):
        token = PASS_TOOLS.set(pass_tools())
        wrapped.live_names()
        wrapped.live_names()
        PASS_TOOLS.reset(token)
    assert runtime.listings == 2


@pytest.mark.feature("fenced-live-names-listing")
def test_live_names_outside_a_pass_lists_every_call() -> None:
    runtime = Listing()
    wrapped = fenced(runtime)
    wrapped.live_names()
    wrapped.live_names()
    assert runtime.listings == 2


@pytest.mark.feature("fenced-live-names-listing")
@pytest.mark.parametrize(
    ("operation", "args", "answer"),
    [
        ("retire", ("engineer@a1b2c3-0001", ("home",)), True),
        ("reap_name", ("engineer@a1b2c3-0001",), True),
        ("close_space", ("config",), True),
        ("close_pane", ("w1:p1",), None),
        ("resume", ("engineer@a1b2c3-0001",), "resumed"),
    ],
)
def test_agent_changes_drop_the_pass_listing(in_pass: Any, operation: str, args: tuple[Any, ...], answer: Any) -> None:
    runtime = Listing()
    wrapped = fenced(runtime)
    wrapped.live_names()
    assert getattr(wrapped, operation)(*args) == answer
    runtime.live.clear()
    assert wrapped.live_names() == set()
    assert runtime.listings == 2
    assert runtime.changes == [(operation, args)]


@pytest.mark.feature("fenced-live-names-listing")
def test_spawn_drops_the_pass_listing(in_pass: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = Listing()
    wrapped = fenced(runtime)
    monkeypatch.setattr(lease, "require_epoch", lambda store, slug, epoch: None)

    def placed(config: Any, lane: str, name: str, task: dict[str, Any]) -> str:
        assert (config, lane, name, task) == ("config", "eng", "engineer@a1b2c3-0002", {"id": "t1"})
        runtime.live.add(name)
        return "placed"

    monkeypatch.setattr(wrapped, "_spawn", placed)
    wrapped.live_names()
    assert wrapped.spawn("config", "eng", "engineer@a1b2c3-0002", {"id": "t1"}) == "placed"  # pyright: ignore[reportArgumentType]
    assert wrapped.live_names() == {"engineer@a1b2c3-0001", "engineer@a1b2c3-0002"}
    assert runtime.listings == 2


@pytest.mark.feature("fenced-live-names-listing")
def test_spawn_drops_the_pass_listing_when_it_fails(in_pass: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = Listing()
    wrapped = fenced(runtime)
    released: list[str] = []
    monkeypatch.setattr(lease, "require_epoch", lambda store, slug, epoch: None)
    monkeypatch.setattr(commands, "hive_id", lambda: "fixture-hive")
    monkeypatch.setattr(
        local_slots, "release", lambda store, machine, slug, name: released.append(f"{machine} {slug} {name}")
    )
    monkeypatch.setattr(admission, "release", lambda redis, machine, key: released.append(f"{redis} {machine} {key}"))

    def failed(config: Any, lane: str, name: str, task: dict[str, Any]) -> str:
        runtime.live.add(name)
        raise RuntimeError("launch failed")

    monkeypatch.setattr(wrapped, "_spawn", failed)
    wrapped.live_names()
    with pytest.raises(RuntimeError, match="^launch failed$"):
        wrapped.spawn("config", "eng", "engineer@a1b2c3-0002", {"id": "t1"})  # pyright: ignore[reportArgumentType]
    assert wrapped.live_names() == {"engineer@a1b2c3-0001", "engineer@a1b2c3-0002"}
    assert runtime.listings == 2
    assert released == [
        "fixture-hive sw engineer@a1b2c3-0002",
        "fixture-redis fixture-hive sw/engineer@a1b2c3-0002",
    ]


@pytest.mark.feature("fenced-live-names-listing")
def test_a_change_through_one_fenced_runtime_drops_the_listing_of_another(in_pass: Any) -> None:
    runtime = Listing()
    first, second = fenced(runtime), fenced(runtime)
    first.live_names()
    second.live_names()
    assert runtime.listings == 1
    first.retire("engineer@a1b2c3-0001")
    runtime.live.clear()
    assert second.live_names() == set()
    assert runtime.listings == 2
