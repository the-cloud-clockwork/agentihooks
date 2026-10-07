import pytest

from scripts.doctor import traces_read


def _page(rows, page, total):
    return {"data": rows, "meta": {"page": page, "totalPages": total}}


def test_reader_walks_every_page_of_traces_and_observations_and_keeps_no_text():
    calls = []
    trace = {"id": "t1", "name": "s-eng-1", "sessionId": "c1", "tags": ["task:a", "swarm:s"], "input": "prompt"}
    tool = {
        "type": "TOOL",
        "name": "Bash",
        "startTime": "2026-10-05T15:00:00.000Z",
        "endTime": "2026-10-05T15:00:01.500Z",
        "metadata": {"attributes": {"error": "true"}},
        "output": "secret text",
    }
    turn = {
        "type": "SPAN",
        "name": "turn 1",
        "startTime": "2026-10-05T15:00:00Z",
        "endTime": None,
        "metadata": {"attributes": {"agent.turn": "1"}},
    }
    generation = {"type": "GENERATION", "name": "m", "startTime": "2026-10-05T15:00:00Z", "totalTokens": 42}

    def get(path, params):
        calls.append((path, params))
        if path == "traces":
            return _page([trace] if params["page"] == 2 else [], params["page"], 2)
        return _page([tool, turn] if params["page"] == 1 else [generation], params["page"], 2)

    [read] = traces_read.traces("s", get)
    assert calls[0] == ("traces", {"tags": "swarm:s", "page": 1, "limit": 100})
    assert ("observations", {"traceId": "t1", "page": 2, "limit": 100}) in calls
    assert read["tags"] == ["swarm:s", "task:a"]
    assert read["session_id"] == "c1"
    assert read["observations"] == [
        {
            "type": "TOOL",
            "name": "Bash",
            "start": 1791212400000,
            "end": 1791212401500,
            "turn": None,
            "error": True,
            "tokens": 0,
        },
        {
            "type": "SPAN",
            "name": "turn 1",
            "start": 1791212400000,
            "end": 1791212400000,
            "turn": 1,
            "error": None,
            "tokens": 0,
        },
        {
            "type": "GENERATION",
            "name": "m",
            "start": 1791212400000,
            "end": 1791212400000,
            "turn": None,
            "error": None,
            "tokens": 42,
        },
    ]
    assert "secret text" not in str(read)


def test_generation_tokens_leave_out_cache_reads():
    usage = {"input": 4, "output": 24, "cache_read_input_tokens": 291980, "cache_creation_input_tokens": 237}
    row = {"type": "GENERATION", "startTime": "2026-10-05T15:00:00Z", "totalTokens": 292245, "usageDetails": usage}
    assert traces_read._observation(row)["tokens"] == 4 + 24 + 237


def test_sessions_join_task_holders_and_live_agents():
    tasks = [
        {"id": "a", "state": "done", "claimed_by": "s-eng-1", "pr_url": "https://github.com/o/r/pull/1"},
        {"id": "b", "state": "open", "claimed_by": "", "pr_url": ""},
        {"id": "c", "state": "done", "claimed_by": "s-eng-2", "pr_url": ""},
    ]
    agents = [{"name": "s-eng-1", "task": "d", "conversation_id": "c9", "started_at": 5}]
    assert traces_read.sessions(tasks, agents) == [
        {"agent": "s-eng-1", "session_id": "c9", "task": "d", "started_at": 5},
        {"agent": "s-eng-2", "session_id": "", "task": "c", "started_at": 0},
    ]
    assert traces_read.merged(tasks) == ["a"]


def test_missing_keys_refuse_instead_of_reporting_no_traces():
    with pytest.raises(RuntimeError, match="LANGFUSE_PUBLIC_KEY"):
        traces_read.client({"LANGFUSE_HOST": "https://x"})
