from types import SimpleNamespace

import pytest
from scripts.swarm_ledger.adapters import hive, hooks, swarm

from scripts.swarm_ledger import composition, ledger_tasks, ledger_verdict, plan_shape, ports


class Refused(Exception):
    pass


@pytest.fixture
def unbound(monkeypatch):
    monkeypatch.setattr(ports, "BOUND", {})


def test_the_composition_root_binds_an_adapter_for_every_port(unbound):
    composition.wire()

    assert isinstance(ports.swarm(), swarm.Swarm)
    assert isinstance(ports.hive(), hive.Hive)
    assert isinstance(ports.hooks(), hooks.Hooks)


@pytest.mark.parametrize(
    ("adapter", "port"),
    [(swarm.Swarm(), ports.Swarm), (hive.Hive(), ports.Hive), (hooks.Hooks(), ports.Hooks)],
)
def test_each_adapter_implements_the_interface_the_ledger_owns(adapter, port):
    assert isinstance(adapter, port)


def test_an_unwired_port_takes_the_composition_root_default(unbound):
    assert isinstance(ports.swarm(), swarm.Swarm)
    assert ports.swarm() is ports.swarm()


def test_a_bound_port_is_kept_when_the_others_are_filled(unbound):
    fake = SimpleNamespace()
    ports.bind(hooks=fake)

    assert isinstance(ports.swarm(), swarm.Swarm)
    assert ports.hooks() is fake


def test_the_ledger_raises_the_error_of_the_bound_swarm(unbound):
    ports.bind(swarm=SimpleNamespace(store=SimpleNamespace(SwarmError=Refused)))

    with pytest.raises(Refused, match="unknown dependencies: t9"):
        plan_shape.analyze([{"id": "t1", "depends_on": ["t9"]}])


def test_the_ledger_asks_the_bound_swarm_for_a_lane(unbound):
    ports.bind(swarm=SimpleNamespace(naming=SimpleNamespace(lane_of=lambda name: "master")))

    assert ledger_tasks._block_author("anyone") is True


def test_the_ledger_screens_a_verdict_through_the_bound_hooks(unbound):
    seen = []
    conditions = SimpleNamespace(contains_condition_signal=lambda ask: seen.append(ask) or True)
    ports.bind(hooks=SimpleNamespace(conditions=conditions))

    assert ledger_verdict.text("approved", "set a condition") == f"approved, {ledger_verdict.CONDITION}"
    assert seen == ["set a condition"]
