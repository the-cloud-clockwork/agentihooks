"""The shared tick helpers live in the common step module and the swarm tick module re-exports each one."""

from types import SimpleNamespace

import pytest

from scripts.swarm import tick
from scripts.tick.steps import common

pytestmark = pytest.mark.unit

MOVED = (
    "skip_refused",
    "skip_dropped",
    "_operator_line",
    "_drop",
    "_follow",
    "_rebind",
    "_collected",
    "_still_held",
    "_ended",
    "_current_owner",
    "_reopen",
)


@pytest.mark.feature("swarm-tick-common-helpers")
@pytest.mark.parametrize("name", MOVED)
def test_tick_reexports_the_common_helper(name: str) -> None:
    helper = getattr(common, name)
    assert helper.__module__ == "scripts.tick.steps.common"
    assert getattr(tick, name) is helper


@pytest.mark.feature("swarm-tick-common-helpers")
def test_common_helpers_reach_tick_seams_through_the_tick_module(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, ...]] = []
    monkeypatch.setattr(tick, "_rebind", lambda *args: seen.append(args))
    agent = SimpleNamespace(name="engineer@1-1")
    assert common._follow("slug", "store", "runtime", agent, 7) == (
        None,
        ["held engineer@1-1 until one pane holds its resumed process 7"],
    )
    assert seen == [("slug", "store", "runtime", agent, 7)]
