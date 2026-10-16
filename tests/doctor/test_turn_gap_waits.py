import pytest

from scripts.doctor import traces


def record(waits):
    return {
        "waits": {"engineer": waits},
        "traces": [
            {
                "name": "engineer",
                "tags": ["agent:engineer"],
                "session_id": "session",
                "observations": [
                    {"type": "SPAN", "turn": 1, "start": 0, "end": 60_000},
                    {"type": "SPAN", "turn": 2, "start": 3_660_000, "end": 3_720_000},
                ],
            }
        ],
    }


@pytest.mark.parametrize("kind", [None, "checks", "reply", "task"])
def test_declared_wait_covers_the_gap(kind):
    held = {"at": 60_000, "until": 3_660_000, "reason": "proof"}
    if kind:
        held["on"] = {"kind": kind, "target": "proof"}
    assert traces.turn_gaps(record([held]), traces.Limits()) == []


@pytest.mark.parametrize("waits", [[], [{"at": 60_000, "until": 3_600_000}], [{"at": 120_000, "until": 3_660_000}]])
def test_uncovered_gap_names_its_bounds_and_wait_result(waits):
    [finding] = traces.turn_gaps(record(waits), traces.Limits())
    assert finding.measure == 60
    assert "turn 1 to turn 2: 60 minutes; gap 60000 to 3660000; no declared wait covered it" in finding.evidence
