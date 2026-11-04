"""The launch cluster lives in the observe and place step modules and the swarm tick module re-exports each name."""

from types import ModuleType, SimpleNamespace

import pytest

from scripts.swarm import tick
from scripts.tick.steps import observe, place

pytestmark = pytest.mark.unit

OBSERVED = (
    "_verify",
    "_launch_checks",
    "_failed_hive_launch",
    "_failed_launch",
    "_retire_launch",
    "_acknowledging",
    "_expire_launch",
    "_finished_launch",
    "_collect_acks",
)
PLACED = (
    "Started",
    "_started",
    "Failed",
    "_spawn_failed",
    "_not_launched",
    "_failed_room_wait",
    "_refused_reservation",
    "_room_refused",
    "_room_wait",
    "_admission",
    "_held_back",
    "_settle_admission",
    "_release_admission",
)
MOVED = [(observe, name) for name in OBSERVED] + [(place, name) for name in PLACED]


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(("module", "name"), MOVED)
def test_tick_reexports_the_moved_launch_name(module: ModuleType, name: str) -> None:
    moved = getattr(module, name)
    assert moved.__module__ == module.__name__
    assert getattr(tick, name) is moved


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_the_tick_module_holds_no_launch_definition() -> None:
    source = (tick.__file__ or "").replace(".pyc", ".py")
    with open(source) as handle:
        text = handle.read()
    assert [name for _, name in MOVED if f"\ndef {name}(" in text or f"\nclass {name}:" in text] == []


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_moved_launch_code_reaches_tick_seams_through_the_tick_module(monkeypatch: pytest.MonkeyPatch) -> None:
    released: list[tuple[str, str, str]] = []
    gate = SimpleNamespace(release=lambda slug, task_id, reservation: released.append((slug, task_id, reservation)))
    monkeypatch.setattr(tick, "_admission", lambda runtime: gate)
    place._release_admission("slug", "runtime", {"id": "t1", "admission": {"reservation": "r1"}})
    assert released == [("slug", "t1", "r1")]


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_an_acknowledging_check_reads_the_launch_through_the_tick_master_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tick, "MASTER", "eng")
    agent = SimpleNamespace(lane="eng", name="engineer@1-1")
    assert observe._acknowledging("slug", "store", agent) is False
