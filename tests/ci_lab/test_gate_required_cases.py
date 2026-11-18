import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

from tests.ci_lab.gate_required_cases import load, materialize

WORKFLOW = Path(__file__).parents[2] / ".github/workflows/test.yml"
SHELL = ("bash", "--noprofile", "--norc", "-eo", "pipefail", "-c")


def _step() -> str:
    jobs: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    return next(step for step in jobs["gate-required"]["steps"] if step.get("name") == "Require every gate to succeed")[
        "run"
    ]


def _passes(script: str, event: str, needs: dict[str, Any], flags: dict[str, str]) -> bool:
    environment = {
        "PATH": os.environ["PATH"],
        "NEEDS": json.dumps(needs),
        "EVENT": event,
        "MUTATION": flags["mutation"],
        "INLINE": flags["inline"],
        "KIND": flags["kind"],
        "REUSED": flags["reused"],
    }
    return subprocess.run([*SHELL, script], env=environment, capture_output=True, text=True).returncode == 0


def test_every_recorded_case_holds_against_todays_gate_required_step() -> None:
    fixture = load()
    script = _step()
    wrong = [
        case["name"] for case in fixture["cases"] if _passes(script, *materialize(fixture, case)) != case["passes"]
    ]
    assert wrong == []


def test_the_recorded_cases_cover_every_event_both_ways() -> None:
    fixture = load()
    seen = {(case.get("event", case["base"]), case["passes"]) for case in fixture["cases"]}
    events = {"push", "pull_request", "merge_group", "workflow_dispatch"}
    assert {event for event, _ in seen} == events
    assert {(event, True) for event in events - {"workflow_dispatch"}} <= seen
    assert {(event, False) for event in events} <= seen
