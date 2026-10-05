import copy

from scripts.doctor import traces
from tests.doctor.recorded import load

LIMITS = traces.Limits()
SWARM = "okay-we-re-going-to-mossy-rabin-2026-10-05"
ENG5 = "lf1-eng5-diagnostic-real-transcript"
ENG11 = "3870a785-6d2e-400e-8293-d45ea183333f"


def _trace(record, session_id):
    return next(t for t in record["traces"] if t["session_id"] == session_id)


def test_recorded_tool_errors_stay_under_the_rate_and_a_planted_burst_raises_it():
    record = load("traces")
    assert traces.tool_errors(record, LIMITS) == []
    planted = copy.deepcopy(record)
    tools = [o for o in _trace(planted, ENG5)["observations"] if o["type"] == "TOOL"]
    for tool in tools[:30]:
        tool["error"] = True
    [found] = traces.tool_errors(planted, LIMITS)
    assert found.id == f"tool-errors/{ENG5}"
    errors = sum(1 for t in tools if t["error"])
    assert found.measure == round(errors * 100 / len(tools))
    assert f"{errors}/{len(tools)} tool calls failed" in found.evidence
    assert f"agent {SWARM}-eng-5" in found.evidence


def test_a_few_calls_cannot_make_a_rate():
    record = load("traces")
    trace = _trace(record, ENG11)
    tools = [o for o in trace["observations"] if o["type"] == "TOOL"]
    trace["observations"] = tools[:3]
    for tool in tools[:3]:
        tool["error"] = True
    assert traces.tool_errors(record, LIMITS) == []


def test_recorded_merged_tasks_report_turns_and_tokens_and_a_planted_heavy_task_is_found():
    record = load("traces")
    costs = {c["task"]: c for c in traces.measures(record)["tasks"]}
    assert set(costs) == set(record["merged"])
    assert costs["lf3"]["turns"] == 5
    assert costs["lf3"]["tokens"] == 9123872
    assert costs["lf2"]["sessions"] == 3
    assert costs["dt1"] == {"task": "dt1", "sessions": 0, "turns": 0, "tokens": 0}
    assert traces.task_cost(record, LIMITS) == []
    planted = copy.deepcopy(record)
    for o in _trace(planted, ENG11)["observations"]:
        o["tokens"] *= 3
    [found] = traces.task_cost(planted, LIMITS)
    assert found.id == "task-cost/lf3"
    assert found.measure == 9123872 * 3
    assert "turns 5" in found.evidence
    assert "traced sessions 1" in found.evidence


def test_unmerged_tasks_never_count_as_merged():
    record = load("traces")
    assert "lf1" not in {c["task"] for c in traces.measures(record)["tasks"]}


def test_recorded_turns_follow_each_other_and_a_planted_long_gap_is_found():
    record = load("traces")
    assert traces.turn_gaps(record, LIMITS) == []
    planted = copy.deepcopy(record)
    for o in _trace(planted, ENG11)["observations"]:
        if o["turn"] in (4, 5):
            o["start"] += 3_600_000
            o["end"] += 3_600_000
    [found] = traces.turn_gaps(planted, LIMITS)
    assert found.id == f"turn-gap/{ENG11}"
    assert found.measure >= 60
    assert any(line.startswith("turn 3 to turn 4: ") for line in found.evidence)


def test_recorded_untraced_sessions_are_named_and_a_planted_trace_clears_one():
    record = load("traces")
    found = {f.subject: f for f in traces.untraced(record, LIMITS)}
    assert f"{SWARM}-master-1" in found
    assert f"{SWARM}-eng-3" in found
    assert f"{SWARM}-eng-11" not in found
    assert f"{SWARM}-eng-12" not in found
    assert f"{SWARM}-eng-13" not in found
    assert "task dt1" in found[f"{SWARM}-eng-3"].evidence
    planted = copy.deepcopy(record)
    planted["traces"].append(
        {"id": "x", "name": "", "session_id": "", "tags": [f"agent:{SWARM}-eng-3"], "observations": []}
    )
    assert f"{SWARM}-eng-3" not in {f.subject for f in traces.untraced(planted, LIMITS)}


def test_a_live_session_past_its_grace_with_no_trace_is_found_by_session_id():
    record = load("traces")
    record["now_ms"] += LIMITS.grace_minutes * 60_000 * 2
    found = {f.subject for f in traces.untraced(record, LIMITS)}
    assert f"{SWARM}-eng-12" in found
    master = next(s for s in record["sessions"] if s["agent"].endswith("master-1"))
    record["traces"][0]["session_id"] = master["session_id"]
    assert f"{SWARM}-master-1" not in {f.subject for f in traces.untraced(record, LIMITS)}


def test_limits_read_positive_whole_numbers_from_the_environment():
    limits = traces.Limits.from_env(
        {"AGENTIHOOKS_DOCTOR_TURN_GAP_MINUTES": "5", "AGENTIHOOKS_DOCTOR_TOOL_ERROR_PCT": "x"}
    )
    assert limits.turn_gap_minutes == 5
    assert limits.tool_error_pct == traces.Limits().tool_error_pct


def test_findings_join_every_kind():
    record = load("traces")
    kinds = {f.kind for f in traces.findings(record, LIMITS)}
    assert kinds == {"untraced session"}
