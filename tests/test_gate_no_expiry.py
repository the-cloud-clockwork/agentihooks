from datetime import date
from typing import Any

import pytest

from scripts import ci_wiring, gate_protected
from scripts.ci_branch_lanes import GROUP


@pytest.mark.feature("gate-declarations-never-expire")
@pytest.mark.parametrize("section", ["not_gates", "outside_gate"])
@pytest.mark.parametrize("expires", [None, "2000-01-01", "2999-01-01", "invalid-date"])
def test_declaration_dates_never_interrupt_wiring_or_protected_grading(section: str, expires: str | None) -> None:
    workflows: dict[str, Any] = {
        "test.yml": {
            "concurrency": {"group": GROUP, "queue": "max", "cancel-in-progress": False},
            "on": {"pull_request": {"branches": ["dev"]}, "merge_group": None, "push": {"branches": ["dev"]}},
            "jobs": {
                "unit": {"runs-on": "ubuntu-latest"},
                "gate": {
                    "name": "Gate — Required",
                    "needs": ["unit"],
                    "if": "${{ always() }}",
                    "steps": [{"run": "jq -e 'all(.[]; .result == \"success\")'"}],
                },
            },
        }
    }
    if section == "not_gates":
        workflows["test.yml"]["jobs"]["report"] = {"runs-on": "ubuntu-latest"}
        key = "test.yml/report"
    else:
        workflows["report.yml"] = {"on": "pull_request", "jobs": {"report": {"runs-on": "ubuntu-latest"}}}
        key = "report.yml/report"
    entry = {"reason": "Reports results without grading them", "owner": "ci"}
    if expires is not None:
        entry["expires"] = expires
    config = {section: {key: entry}}
    today = date(9999, 12, 31)
    assert ci_wiring.check(workflows, config, today) == []
    assert gate_protected.grade(workflows, config, workflows, config, today) == []
