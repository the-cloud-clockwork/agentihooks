import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts import ci_budget, ci_wiring
from scripts.ci_lab import static_gates
from tests.contracts.ci_tiers.test_workflow import enabled

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/test.yml"
GRADING_STEP = "Hold the strict type checker's errors per file to the base baseline"


def _jobs() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


@pytest.mark.feature("type-gate-split-by-tier")
def test_the_type_gate_grading_step_sets_direct_importers_on_itself_not_the_job() -> None:
    jobs = _jobs()
    grade = next(step for step in jobs["type-gate"]["steps"] if step.get("name") == GRADING_STEP)
    assert grade.get("env", {}).get("TYPE_GATE_DIRECT_IMPORTERS") == "1"
    assert "TYPE_GATE_DIRECT_IMPORTERS" not in jobs["type-gate"].get("env", {})


@pytest.mark.feature("type-gate-split-by-tier")
def test_the_lab_discovers_the_same_direct_importers_scope_from_the_grading_step() -> None:
    gates = static_gates.discover(WORKFLOW.read_text(), reusable=ci_wiring.load(ROOT))
    gate = next(gate for gate in gates if gate.job == "type-gate" and gate.step == GRADING_STEP)
    assert ("TYPE_GATE_DIRECT_IMPORTERS", "1") in gate.env


def test_the_whole_tree_type_check_runs_from_the_checked_out_tree_on_a_push_to_dev() -> None:
    jobs = _jobs()
    tree = jobs["type-gate-tree"]
    assert any(step.get("run") == "python -m scripts.type_gate --tree" for step in tree["steps"])
    assert enabled(tree["if"], "push", {})
    assert not enabled(tree["if"], "push", {}, ref="refs/heads/other")
    for event in ("pull_request", "merge_group"):
        assert not enabled(tree["if"], event, {})
    admitted = {"admission": SimpleNamespace(result="success")}
    refused = {"admission": SimpleNamespace(result="failure")}
    assert enabled(tree["if"], "workflow_dispatch", admitted)
    assert not enabled(tree["if"], "workflow_dispatch", refused)


def test_the_whole_tree_type_check_gates_promotion_but_not_gate_required() -> None:
    jobs = _jobs()
    assert "type-gate-tree" in jobs["promotion-ready"]["needs"]
    assert "type-gate-tree" in jobs["promote"]["needs"]
    assert "needs.type-gate-tree.result == 'success'" in jobs["promotion-ready"]["if"]
    assert "needs.type-gate-tree.result == 'success'" in jobs["promote"]["if"]
    assert "type-gate-tree" not in jobs["gate-required"]["needs"]


def test_the_whole_tree_type_check_s_runner_class_and_budget_are_registered() -> None:
    classes = json.loads((ROOT / ".github/runner-classes.json").read_text())
    assert classes["test.yml/type-gate-tree"] == classes["test.yml/type-gate"]
    assert ci_budget.BUDGETS["type-gate-tree"] >= ci_budget.BUDGETS["type-gate"]
