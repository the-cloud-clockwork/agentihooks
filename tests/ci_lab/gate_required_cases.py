"""Recorded `Gate — Required` cases: needs recorded from Actions runs, each case a change to one recorded base."""

import copy
import json
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).parents[1] / "fixtures/ci_lab/gate_required.json"


def load() -> dict[str, Any]:
    """Return the recorded bases and cases."""
    return json.loads(FIXTURE.read_text())


def materialize(fixture: dict[str, Any], case: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, str]]:
    """Return a case's event, needs and flags: its base's recording with the case's deletions, records, results and
    outputs applied in that order."""
    base = fixture["bases"][case["base"]]
    needs: dict[str, Any] = copy.deepcopy(base["needs"])
    for job in case.get("delete", []):
        needs.pop(job, None)
    for job, record in case.get("set", {}).items():
        needs[job] = record
    for job, result in case.get("results", {}).items():
        needs[job] = {**needs.get(job, {"outputs": {}}), "result": result}
    for job, outputs in case.get("outputs", {}).items():
        needs[job] = {**needs.get(job, {"result": "success"}), "outputs": outputs}
    return case.get("event", case["base"]), needs, {**base["flags"], **case.get("flags", {})}
