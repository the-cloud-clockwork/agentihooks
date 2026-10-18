import pytest

from scripts.doctor import traces, traces_read


def _page(rows, pages):
    return {"data": rows, "meta": {"totalPages": pages}}


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
    assert coverage == {"listed": True, "traces": 1, "covered": 1, "complete": True, "unreadable": []}
    assert failures == []
