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


def _quiet(name):
    return {
        "name": name,
        "tags": [f"agent:{name}"],
        "session_id": f"{name}-session",
        "observations": [
            {"type": "SPAN", "turn": 1, "start": 0, "end": 60_000},
            {"type": "SPAN", "turn": 2, "start": 1_860_000, "end": 1_920_000},
        ],
    }


def test_only_agents_with_a_gap_over_the_limit_need_their_wait_history():
    sample = record([])
    sample["traces"] += [_quiet("ci"), {**_quiet("engineer"), "session_id": "again"}]
    assert traces.gap_agents(sample, traces.Limits()) == ["engineer"]
    assert traces.gap_agents(sample, traces.Limits(turn_gap_minutes=29)) == ["engineer", "ci"]


def test_wait_history_is_read_newest_gap_first_then_by_name():
    sample = {"traces": [_quiet("dev"), _quiet("ci"), record([])["traces"][0]]}
    assert traces.gap_agents(sample, traces.Limits(turn_gap_minutes=29)) == ["engineer", "ci", "dev"]


def test_live_agents_are_read_before_retired_ones_each_newest_gap_first():
    sample = {"traces": [_quiet("dev"), _quiet("ci"), record([])["traces"][0]], "active": [{"agent": "dev"}]}
    assert traces.gap_agents(sample, traces.Limits(turn_gap_minutes=29)) == ["dev", "engineer", "ci"]
    sample["active"].append({"agent": "engineer"})
    assert traces.gap_agents(sample, traces.Limits(turn_gap_minutes=29)) == ["engineer", "dev", "ci"]


def test_the_partial_read_counts_the_live_agents_left_unread():
    sample = {**record([]), "slug": "sw", "waits_unread": ["engineer", "ci", "dev"], "active": [{"agent": "ci"}]}
    [partial] = traces.wait_coverage(sample, traces.Limits())
    assert partial.evidence[1] == "live agents unread: 1"
    sample["active"].append({"agent": "dev"})
    assert traces.wait_coverage(sample, traces.Limits())[0].evidence[1] == "live agents unread: 2"


def test_an_agent_whose_wait_history_was_not_read_is_not_judged_and_the_read_is_partial():
    sample = {**record([]), "slug": "sw", "waits": {}, "waits_unread": ["engineer"]}
    assert traces.turn_gaps(sample, traces.Limits()) == []
    judged = {**record([])["traces"][0], "name": "ci", "tags": ["agent:ci"], "session_id": "ci-session"}
    after = traces.turn_gaps({**sample, "traces": [*sample["traces"], judged]}, traces.Limits())
    assert [f.subject for f in after] == ["ci-session"]
    [partial] = traces.wait_coverage(sample, traces.Limits())
    assert (partial.kind, partial.subject, partial.measure) == ("wait history partial", "sw", 1)
    assert partial.summary == "wait history of 1 agent with turn gaps over 30 minutes not read in budget"
    assert partial.threshold == "every agent with a turn gap over the limit has its wait history read"
    two = traces.wait_coverage({**sample, "waits_unread": ["engineer", "ci"]}, traces.Limits())[0]
    assert two.summary == "wait history of 2 agents with turn gaps over 30 minutes not read in budget"
    assert two.evidence[0] == "unread: engineer, ci"
    six = traces.wait_coverage({**sample, "waits_unread": list("abcdef")}, traces.Limits())[0]
    assert (six.evidence[0], six.measure) == ("unread: a, b, c, d, e", 6)
    assert partial.evidence == (
        "unread: engineer",
        "live agents unread: 0",
        "turn gap findings skip agents whose wait history was not read",
        "each Doctor scan reads the wait history again within its budget",
    )
    assert partial in traces.findings({**sample, "sessions": [], "merged": [], "now_ms": 0}, traces.Limits())


def test_a_whole_wait_read_reports_no_partial_read():
    assert traces.wait_coverage(record([]), traces.Limits()) == []
    assert traces.wait_coverage({**record([]), "waits_unread": []}, traces.Limits()) == []
