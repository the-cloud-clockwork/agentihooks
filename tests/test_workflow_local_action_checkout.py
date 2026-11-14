"""Always run local action steps find their action because checkout cannot be skipped before them."""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _always(step: dict[str, Any]) -> bool:
    """Return whether a step runs whatever the earlier steps did."""
    return "always()" in str(step.get("if", ""))


def _provides_actions(step: dict[str, Any]) -> bool:
    """Return whether a step puts the repository's local actions in the workspace."""
    return str(step.get("uses", "")).startswith("actions/checkout") or ".github/actions" in str(step.get("run", ""))


def unguarded_local_actions(jobs: dict[str, Any]) -> list[tuple[str, int]]:
    """List each always run local action step whose providing step a failed earlier step can skip."""
    found = []
    for name, job in jobs.items():
        steps = (job or {}).get("steps") or []
        for index, step in enumerate(steps):
            if not str(step.get("uses", "")).startswith("./") or not _always(step):
                continue
            provider = next((at for at, earlier in enumerate(steps[:index]) if _provides_actions(earlier)), None)
            skippable = provider is not None and not _always(steps[provider]) and not all(map(_always, steps[:provider]))
            if provider is None or skippable:
                found.append((name, index))
    return found


def _workflows() -> list[Path]:
    """Return every workflow file in the repository."""
    return sorted((ROOT / ".github/workflows").glob("*.y*ml"))


@pytest.mark.feature("ci-local-action-checkout-first")
def test_every_always_run_local_action_follows_a_checkout_no_failure_can_skip() -> None:
    offenders = {
        workflow.name: unguarded_local_actions(yaml.safe_load(workflow.read_text()).get("jobs") or {})
        for workflow in _workflows()
    }
    assert {name: found for name, found in offenders.items() if found} == {}


@pytest.mark.feature("ci-local-action-checkout-first")
def test_a_gate_step_before_checkout_is_flagged() -> None:
    jobs = {
        "compare": {
            "steps": [
                {"name": "gate", "run": "false"},
                {"uses": "actions/checkout@v4"},
                {"uses": "./.github/actions/retry-upload", "if": "always()"},
            ]
        }
    }
    assert unguarded_local_actions(jobs) == [("compare", 2)]


@pytest.mark.feature("ci-local-action-checkout-first")
def test_checkout_first_or_always_provided_actions_pass() -> None:
    jobs = {
        "first": {
            "steps": [
                {"uses": "actions/checkout@v4"},
                {"name": "gate", "run": "false"},
                {"uses": "./.github/actions/retry-upload", "if": "always()"},
            ]
        },
        "archived": {
            "steps": [
                {"name": "gate", "run": "false"},
                {"if": "always()", "run": "git archive HEAD .github/actions/retry-upload | tar -x"},
                {"uses": "./.workflow-revision/.github/actions/retry-upload", "if": "always()"},
            ]
        },
        "none": {"steps": [{"uses": "./.github/actions/retry-upload", "if": "always()"}]},
    }
    assert unguarded_local_actions(jobs) == [("none", 0)]
