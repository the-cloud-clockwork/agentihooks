"""The launch cluster lives in the observe and place step modules and the swarm tick module re-exports each name."""

from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scripts.swarm import tick
from scripts.swarm.store import MASTER
from scripts.tick.steps import observe, place

pytestmark = pytest.mark.unit

OBSERVED = (
    "_verify",
    "_launch_checks",
    "_failed_hive_launch",
    "_failed_launch",
    "_retire_launch",
    "_acknowledging",
)
PLACED = (
    "_admission",
    "_settle_admission",
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
    text = Path(str(tick.__file__)).read_text(encoding="utf-8")
    assert [name for _, name in MOVED if f"\ndef {name}(" in text or f"\nclass {name}:" in text] == []


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_an_acknowledging_check_reads_the_launch_through_the_tick_master_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = SimpleNamespace(lane=MASTER, name="master@1-1")
    assert observe._acknowledging("slug", "store", agent) is False
