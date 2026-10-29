import pytest

from scripts.doctor import traces, traces_read


def _page(rows, pages):
    return {"data": rows, "meta": {"totalPages": pages, "totalItems": len(rows) * pages}}


@pytest.mark.parametrize("repeated", ["traces", "observations"])
def test_repeated_history_pages_count_each_session_and_generation_once(tmp_path, repeated):
    def get(path, params):
        if path == "traces":
            return _page(
                [{"id": "session", "sessionId": "session", "tags": ["task:large"], "updatedAt": "u"}],
                2 if repeated == path else 1,
            )
        return _page(
            [
                {"id": "generation", "type": "GENERATION", "totalTokens": 568388},
                {"id": "turn", "type": "SPAN", "metadata": {"attributes": {"agent.turn": "1"}}},
            ],
            2 if repeated == path else 1,
        )

    found, coverage, failures = traces_read.history("s", get, tmp_path, traces_read.Budget())
    record = {"now_ms": 0, "merged": ["large"], "traces": found}
    assert traces.measures(record)["tasks"] == [{"task": "large", "sessions": 1, "turns": 1, "tokens": 568388}]
    assert coverage == {
        "listed": True,
        "unlisted": 0,
        "traces": 1,
        "covered": 1,
        "capped": 0,
        "complete": True,
        "unreadable": [],
    }
    assert failures == []


@pytest.mark.parametrize("cap,median,truncated", [(1, 400, True), (2, 200, False)])
def test_task_cost_names_a_truncated_comparison_median(tmp_path, cap, median, truncated):
    tokens = {"large": 1000, "medium": 400, "small": 100, "other": 200}

    def get(path, params):
        if path == "traces":
            tasks = ["large", "medium"] if params["page"] == 1 else ["small", "other"]
            return _page([{"id": task, "tags": [f"task:{task}"], "updatedAt": "u"} for task in tasks], 2)
        return _page(
            [
                {"id": "generation", "type": "GENERATION", "totalTokens": tokens[params["traceId"]]},
                {"id": "turn", "type": "SPAN", "metadata": {"attributes": {"agent.turn": "1"}}},
            ],
            1,
        )

    found, coverage, failures = traces_read.history("s", get, tmp_path, traces_read.Budget(trace_pages=cap))
    record = {"merged": list(tokens), "traces": found, "reader": {"historical": coverage}}
    [finding] = traces.task_cost(record, traces.Limits())
    assert finding.subject == "large"
    assert finding.measure == 1000
    assert f"uncached tokens 1000, median of the other merged tasks {median}" in finding.evidence
    assert ("comparison set truncated: trace listing incomplete" in finding.evidence) is truncated
    assert failures == []


def test_old_cached_usage_is_read_again_before_task_cost_is_reported(tmp_path):
    from scripts.doctor import registry

    registry.save(
        tmp_path / "session.json",
        {
            "updated_at": "u",
            "whole": True,
            "observations": [
                {"type": "GENERATION", "tokens": 568388},
                {"type": "GENERATION", "tokens": 568388},
                {"type": "SPAN", "turn": 1},
            ],
        },
    )

    def get(path, params):
        if path == "traces":
            return _page([{"id": "session", "tags": ["task:large"], "updatedAt": "u"}], 1)
        return _page(
            [
                {"id": "generation", "type": "GENERATION", "totalTokens": 568388},
                {"id": "turn", "type": "SPAN", "metadata": {"attributes": {"agent.turn": "1"}}},
            ],
            1,
        )

    found, coverage, failures = traces_read.history("s", get, tmp_path, traces_read.Budget())
    record = {"now_ms": 0, "merged": ["large"], "traces": found}
    assert traces.measures(record)["tasks"] == [{"task": "large", "sessions": 1, "turns": 1, "tokens": 568388}]
    assert coverage["complete"] is True
    assert failures == []
