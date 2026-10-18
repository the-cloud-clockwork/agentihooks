import pytest

from scripts.doctor import traces, traces_read


def _trace(task, tokens, *, complete=True, turns=True):
    observations = [{"type": "GENERATION", "tokens": tokens}]
    if turns:
        observations.append({"type": "SPAN", "turn": 1})
    return {
        "id": task,
        "name": task,
        "session_id": task,
        "tags": [f"task:{task}"],
        "complete": complete,
        "observations": observations,
    }


@pytest.mark.parametrize("complete,turns", [(False, True), (False, False), (True, False)])
def test_unreliable_task_costs_cannot_raise_findings_or_lower_the_median(complete, turns):
    record = {
        "now_ms": 0,
        "merged": ["partial", "small", "medium", "large", "partial2", "partial3"],
        "traces": [
            _trace("small", 100),
            _trace("medium", 150),
            _trace("large", 250),
            _trace("partial", 1, complete=complete, turns=turns),
            _trace("partial2", 1, complete=complete, turns=turns),
            _trace("partial3", 1, complete=complete, turns=turns),
        ],
    }
    assert traces.task_cost(record, traces.Limits()) == []
    assert {cost["task"] for cost in traces.measures(record)["tasks"]} == {"small", "medium", "large"}


@pytest.mark.parametrize("complete,turns", [(False, True), (False, False), (True, False)])
def test_one_unreliable_session_excludes_the_whole_task_cost(complete, turns):
    record = {
        "now_ms": 0,
        "merged": ["small", "large"],
        "traces": [
            _trace("small", 100),
            _trace("large", 500),
            _trace("large", 1, complete=complete, turns=turns),
        ],
    }
    assert traces.task_cost(record, traces.Limits()) == []
    assert traces.measures(record)["tasks"] == [{"task": "small", "sessions": 1, "turns": 1, "tokens": 100}]


def test_complete_task_cost_keeps_every_session_and_turn():
    record = {
        "now_ms": 0,
        "merged": ["small", "large"],
        "traces": [_trace("small", 100), _trace("large", 300), _trace("large", 500)],
    }
    [finding] = traces.task_cost(record, traces.Limits())
    assert finding.subject == "large"
    assert finding.measure == 800
    assert "turns 2" in finding.evidence
    assert "traced sessions 2" in finding.evidence


@pytest.mark.parametrize("pages,complete", [(1, True), (2, False)])
def test_history_marks_each_trace_complete_for_task_costs(tmp_path, pages, complete):
    def get(path, params):
        if path == "traces":
            return {"data": [{"id": "a", "tags": ["task:large"], "updatedAt": "u"}], "meta": {"totalPages": 1}}
        return {
            "data": [{"type": "GENERATION", "totalTokens": 500, "usageDetails": {}}],
            "meta": {"totalPages": pages},
        }

    [trace], coverage, failures = traces_read.history("s", get, tmp_path, traces_read.Budget(observation_pages=1))
    assert failures == []
    assert coverage["covered"] == int(complete)
    assert trace["complete"] is complete
    [cached], _, _ = traces_read.history("s", get, tmp_path, traces_read.Budget(observation_pages=1))
    assert cached["complete"] is complete
