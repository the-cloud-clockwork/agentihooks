import pytest

from scripts.doctor import rates

pytestmark = pytest.mark.feature("swarm-review-lane-reporting")
REVIEWER = "reviewer@abcdef-0001"


@pytest.mark.feature("swarm-review-lane-reporting")
def test_doctor_counts_reviewer_talk_and_monitoring() -> None:
    records = rates.Records(
        events=[{"kind": "comment added", "target": "phases/p", "by": REVIEWER, "at": 110}],
        tasks={},
        activity={REVIEWER: [{"kind": "watch", "at": 110}, {"kind": "act", "at": 120}]},
        findings={},
        gate_log=[],
        injections=[],
        corrections=[],
        pulls={},
    )
    window = rates.Window(100, 200)
    assert rates.ceremony(records, window) == {"talk writes": 1, "outcomes": 0, "talk per outcome": None}
    assert rates.monitoring(records, window) == {
        "worker watch calls": 1,
        "worker actions": 1,
        "worker watch per action": 1.0,
        "master watch per action": None,
    }
    assert not rates._worker("master@abcdef-0002")
