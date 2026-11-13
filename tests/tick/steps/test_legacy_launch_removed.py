"""The tick steps and helpers that served only the legacy hive launch path stay deleted from every tick artefact."""

import importlib
import json
from pathlib import Path

import pytest

from scripts.swarm import launch_job, successor_retry
from scripts.swarm import tick as tick_module
from scripts.tick import registry
from scripts.tick.steps import agents, budgets, observe, place

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
REMOVED_STEPS = ("collect acks", "preempt", "orphans", "spawn due")
REMOVED_SYMBOLS = (
    (tick_module, "_collect_acks"),
    (tick_module, "_expire_launch"),
    (tick_module, "_finished_launch"),
    (tick_module, "ACK_EXPIRED"),
    (observe, "_collect_acks"),
    (observe, "_expire_launch"),
    (observe, "_finished_launch"),
    (observe, "step_spawn_due"),
    (place, "_collect_acks"),
    (place, "_preempt"),
    (place, "PREEMPT_NEEDS"),
    (agents, "_orphans"),
    (agents, "_orphans_step"),
    (launch_job, "collect"),
    (launch_job, "read"),
    (launch_job, "completed"),
    (launch_job, "_failure"),
    (successor_retry, "relaunch"),
)


@pytest.mark.parametrize("name", REMOVED_STEPS)
def test_a_removed_legacy_step_is_not_registered(name: str) -> None:
    assert name not in {step.name for step in registry.steps()}


@pytest.mark.parametrize("name", REMOVED_STEPS)
def test_a_removed_legacy_step_has_no_budget_floor(name: str) -> None:
    assert name not in budgets.FLOORS


@pytest.mark.parametrize("name", REMOVED_STEPS)
def test_a_removed_legacy_step_has_no_scenario_row(name: str) -> None:
    index = json.loads((ROOT / "tests/swarm/tick_scenarios/steps.json").read_text())
    assert name not in index["unmapped"]
    assert name not in index["scenarios"]


@pytest.mark.parametrize("name", REMOVED_STEPS)
def test_no_registered_step_waits_on_a_removed_legacy_step(name: str) -> None:
    assert [step.name for step in registry.steps() if name in step.needs] == []


@pytest.mark.parametrize("name", REMOVED_STEPS)
def test_spawn_needs_no_removed_legacy_step(name: str) -> None:
    assert name not in budgets.SPAWN_NEEDS


@pytest.mark.parametrize(("module", "name"), REMOVED_SYMBOLS, ids=lambda value: getattr(value, "__name__", value))
def test_a_removed_legacy_launch_symbol_stays_deleted(module: object, name: str) -> None:
    assert not hasattr(module, name)


def test_the_scheduled_acknowledgement_pass_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.swarm.ack_pass")
