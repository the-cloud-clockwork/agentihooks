"""A raising intent check ends its tick pass failed only after the steps that never needed its verdict ran."""

from typing import Any

import pytest

from scripts.gates import intent
from scripts.swarm import cli
from scripts.tick import sites
from scripts.tick.steps import PassBox
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick_groups import _Marker, _recorder, _with

pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-tick-intent-failure-held")
def test_a_raising_intent_check_still_runs_the_steps_that_never_needed_it(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    failure = RuntimeError("intent failed")
    ran: list[str] = []

    class Failing:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def run(self, doc: Any) -> list[str]:
            raise failure

    monkeypatch.setattr(intent, "Check", Failing)
    _with(
        monkeypatch,
        _Marker("relay marker", "wrapper relay", _recorder(ran, lambda get: "relay")),
        _Marker("metrics marker", "wrapper metrics", _recorder(ran, lambda get: "metrics")),
    )
    actions = cli.run_tick(swarm, "sw", ledger, runtime, FakeHerdr({}))
    assert actions == [f"pass failed: a step raised RuntimeError: {failure}"]
    assert sorted(ran) == ["metrics", "relay"]


@pytest.mark.feature("swarm-tick-intent-failure-held")
def test_the_relay_hook_leaves_a_raised_intent_check_to_the_pass_end() -> None:
    wrapper: Any = object.__new__(cli._Wrapper)
    wrapper.box = PassBox()
    wrapper.judging = PassBox({"raised": RuntimeError("intent failed")})
    wrapper.checked = []
    wrapper.ticks = sites.TickSites({}, PassBox(), {}, lambda: 0)
    assert wrapper.hooks()["wrapper relay"]() is True
    assert dict(wrapper.box) == {}
    assert wrapper.checked == []
