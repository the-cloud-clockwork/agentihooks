from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import (
    composition,
    ledger_authority,
    ledger_duplicates,
    ledger_tasks,
    ledger_verdict,
    plan_shape,
    ports,
)
from scripts.swarm_ledger.adapters import hive, hooks, swarm

REAL_DECIDE = ledger_duplicates.decide


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


def test_the_ledger_connects_through_the_bound_swarm_store(unbound):
    ports.bind(swarm=SimpleNamespace(store=SimpleNamespace(connect=lambda environ=None: ("store", environ))))

    assert ledger_authority.connect({"SWARM": "x"}) == ("store", {"SWARM": "x"})


def test_the_ledger_decides_duplicates_through_the_bound_hooks(monkeypatch, unbound):
    monkeypatch.setattr(ledger_duplicates, "decide", REAL_DECIDE)
    calls = []

    def decide(*args, **kwargs):
        calls.append((args, kwargs))
        return "verdict"

    ports.bind(hooks=SimpleNamespace(classifier=SimpleNamespace(decide=decide)))

    assert ledger_duplicates.decide("purpose", state=1) == "verdict"
    assert calls == [(("purpose",), {"state": 1})]


def test_the_duplicate_rule_is_built_from_the_bound_hooks(unbound):
    code_rules = SimpleNamespace(CodeRule=lambda *parts: parts, asked="asked")
    ports.bind(hooks=SimpleNamespace(classifier=SimpleNamespace(code_rules=code_rules)))

    assert ledger_duplicates.RULE == (
        "asked",
        ledger_duplicates._verdicts,
        {"duplicate": (True, False)},
        {"duplicate": False},
    )
    with pytest.raises(AttributeError, match="NOT_A_RULE"):
        ledger_duplicates.__getattr__("NOT_A_RULE")
