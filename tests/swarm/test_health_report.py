import pytest

from scripts.swarm.health import report

FINDING = {
    "kind": "stale-claim",
    "subject": "t9",
    "summary": "claimed with no ledger change",
    "evidence": ["45 minutes since the last change", "claimed by engineer@a-1"],
    "threshold": "30 minutes",
    "id": "stale-claim/t9",
    "verdict": None,
}


@pytest.mark.feature("swarm-health-false-positives")
def test_the_false_positives_line_leads_the_findings() -> None:
    counts = {"ceremony": {"false_positive": 2, "judged": 3}, "stale-claim": {"false_positive": 0, "judged": 1}}
    judged = {**FINDING, "verdict": {"value": "false-positive"}}
    assert report.text([FINDING, judged], counts) == (
        "false positives  ceremony 2 of 3 judged  stale-claim 0 of 1 judged\n"
        "finding  stale-claim  t9: claimed with no ledger change\n"
        "  - 45 minutes since the last change\n"
        "  - claimed by engineer@a-1\n"
        "  threshold 30 minutes\n"
        "  id stale-claim/t9\n"
        "finding  stale-claim  t9: claimed with no ledger change\n"
        "  - 45 minutes since the last change\n"
        "  - claimed by engineer@a-1\n"
        "  threshold 30 minutes\n"
        "  id stale-claim/t9  earlier verdict false-positive\n"
    )


@pytest.mark.feature("swarm-health-false-positives")
def test_no_judged_finding_prints_no_false_positives_line() -> None:
    assert report.text([], {}) == ""
    assert report.text([FINDING], {}).splitlines()[0] == "finding  stale-claim  t9: claimed with no ledger change"
