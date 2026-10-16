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
    assert finding.summary == "1 gaps between turns over 30 minutes without a covering declared wait"
    assert "turn 1 to turn 2: 60 minutes; gap 60000 to 3660000; no declared wait covered it" in finding.evidence


@pytest.mark.parametrize(
    "waits, covered",
    [
        ([{"at": 1_860_000, "until": 3_660_000}, {"at": 60_000, "until": 1_860_000}], True),
        ([{"at": 60_000, "until": 1_860_000}, {"at": 1_860_001, "until": 3_660_000}], False),
        ([{"at": 59_999, "until": 3_660_001}], True),
        ([{"at": 0, "until": 59_999}, {"at": 60_001, "until": 3_660_000}], False),
    ],
)
def test_renewed_waits_must_cover_the_whole_gap(waits, covered):
    found = traces.turn_gaps(record(waits), traces.Limits())
    assert bool(found) is not covered


@pytest.mark.parametrize("gap_ms, flagged", [(1_800_000, False), (1_859_999, False), (1_860_000, True)])
def test_gap_threshold_keeps_existing_whole_minute_precision(gap_ms, flagged):
    sample = record([])
    sample["traces"][0]["observations"][1].update(start=60_000 + gap_ms, end=120_000 + gap_ms)
    assert bool(traces.turn_gaps(sample, traces.Limits())) is flagged
