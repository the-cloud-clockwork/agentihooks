import json

import httpx
import pytest

from scripts.doctor import traces, traces_read


def _at(raiser, line=1):
    return f" at {raiser.__code__.co_filename}:{raiser.__code__.co_firstlineno + line}"


def _page(rows, page, total, items=None):
    return {
        "data": rows,
        "meta": {"page": page, "totalPages": total, "totalItems": len(rows) if items is None else items},
    }


def test_reader_walks_every_page_of_traces_and_observations_and_keeps_no_text(tmp_path):
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

    [read], coverage, failures = traces_read.history("s", get, tmp_path, traces_read.Budget())
    assert calls[0] == ("traces", {"tags": "swarm:s", "fields": "core", "page": 1, "limit": 100})
    assert ("observations", {"traceId": "t1", "page": 2, "limit": traces_read.OBSERVATION_PAGE}) in calls
    assert traces_read.OBSERVATION_PAGE == 50
    assert (coverage, failures) == (
        {"listed": True, "unlisted": 0, "traces": 1, "covered": 1, "capped": 0, "complete": True, "unreadable": []},
        [],
    )
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
    assert "secret text" not in "".join(p.read_text() for p in tmp_path.rglob("*.json"))


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
    get = traces_read.client({"LANGFUSE_HOST": "https://x"})
    with pytest.raises(RuntimeError, match="LANGFUSE_PUBLIC_KEY"):
        get("traces", {})


def _trace_row(trace_id, updated):
    return {"id": trace_id, "name": "s-eng-1", "sessionId": trace_id, "tags": ["swarm:s"], "updatedAt": updated}


class Langfuse:
    def __init__(self, traces, pages=1, items=None):
        self.traces, self.pages, self.items, self.calls = traces, pages, items, []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "traces":
            assert sum(1 for call in self.calls if call[0] == "traces") <= traces_read.Budget().trace_pages * 3
            rows = self.traces if params["page"] == 1 else []
            return _page(rows, params["page"], self.pages, len(self.traces) if self.items is None else self.items)
        row = {"type": "TOOL", "name": params["traceId"], "startTime": "2026-10-05T15:00:00Z"}
        return _page([row], params["page"], 1)


def _ticks(step):
    now = [0.0]

    def clock():
        now[0] += step
        return now[0]

    return clock


def test_unchanged_traces_are_read_from_the_cache_on_the_next_scan(tmp_path):
    langfuse = Langfuse([_trace_row("a", "u1"), _trace_row("b", "u1")])
    traces_read.history("s", langfuse, tmp_path, traces_read.Budget())
    langfuse.calls.clear()
    langfuse.traces = [_trace_row("a", "u1"), _trace_row("b", "u2")]
    rows, coverage, _ = traces_read.history("s", langfuse, tmp_path, traces_read.Budget())
    assert [c[1].get("traceId") for c in langfuse.calls if c[0] == "observations"] == ["b"]
    assert [r["observations"][0]["name"] for r in rows] == ["a", "b"]
    assert coverage["complete"] is True


def test_the_history_budget_leaves_the_rest_for_a_later_scan_and_says_so(tmp_path):
    langfuse = Langfuse([_trace_row(f"t{n}", "u") for n in range(4)])
    budget = traces_read.Budget(history_seconds=2)
    rows, coverage, failures = traces_read.history("s", langfuse, tmp_path, budget, clock=_ticks(1))
    assert coverage == {
        "listed": True,
        "unlisted": 0,
        "traces": 4,
        "covered": 1,
        "capped": 0,
        "complete": False,
        "unreadable": [],
    }
    assert failures == []
    assert [len(r["observations"]) for r in rows] == [1, 0, 0, 0]
    rows, coverage, _ = traces_read.history("s", langfuse, tmp_path, traces_read.Budget())
    assert coverage["covered"] == 4


def test_a_single_trace_stops_at_the_budget_without_caching_partial_history(tmp_path):
    now = [0.0]
    pages = []

    def get(path, params):
        if path == "traces":
            return _page([_trace_row("a", "u")], 1, 1)
        pages.append(params["page"])
        now[0] += 5
        return _page(
            [{"type": "TOOL", "name": f"page {params['page']}", "startTime": "2026-10-05T15:00:00Z"}],
            params["page"],
            4,
        )

    budget = traces_read.Budget(history_seconds=10)
    [row], coverage, failures = traces_read.history("s", get, tmp_path, budget, clock=lambda: now[0])
    assert pages == [1, 2]
    assert now[0] == 10
    assert row["complete"] is False
    assert len(row["observations"]) == 2
    assert coverage == {
        "listed": True,
        "unlisted": 0,
        "traces": 1,
        "covered": 0,
        "capped": 0,
        "complete": False,
        "unreadable": [],
    }
    assert failures == []
    assert list(tmp_path.rglob("*.json")) == []

    pages.clear()
    [row], coverage, failures = traces_read.history(
        "s", get, tmp_path, traces_read.Budget(history_seconds=30), clock=lambda: now[0]
    )
    assert pages == [1, 2, 3, 4]
    assert row["complete"] is True
    assert [o["name"] for o in row["observations"]] == ["page 1", "page 2", "page 3", "page 4"]
    assert coverage["complete"] is True
    assert failures == []

    pages.clear()
    [row], coverage, failures = traces_read.history("s", get, tmp_path, budget, clock=lambda: now[0])
    assert pages == []
    assert row["complete"] is True
    assert len(row["observations"]) == 4
    assert coverage["complete"] is True
    assert failures == []


def test_the_trace_listing_stops_at_its_page_cap(tmp_path):
    langfuse = Langfuse([_trace_row("a", "u")], pages=50)
    _, coverage, failures = traces_read.history("s", langfuse, tmp_path, traces_read.Budget(trace_pages=3))
    assert [c[1]["page"] for c in langfuse.calls if c[0] == "traces"] == [1, 2, 3]
    assert coverage["listed"] is False and coverage["complete"] is True
    assert failures == []


def test_a_trace_with_more_observation_pages_than_the_cap_is_not_covered(tmp_path):
    def get(path, params):
        if path == "traces":
            return _page([_trace_row("a", "u")], 1, 1)
        return _page([{"type": "TOOL", "startTime": "2026-10-05T15:00:00Z"}], params["page"], 9)

    budget = traces_read.Budget(observation_pages=2)
    [row], coverage, _ = traces_read.history("s", get, tmp_path, budget)
    assert len(row["observations"]) == 2 and coverage["covered"] == 0


def test_a_failed_listing_is_a_failure_and_not_an_empty_swarm(tmp_path):
    def down(path, params):
        raise TimeoutError("read timed out")

    rows, coverage, failures = traces_read.history("s", down, tmp_path, traces_read.Budget())
    assert rows == [] and coverage["listed"] is False
    assert failures == [f"trace listing failed: TimeoutError: read timed out{_at(down)}"]


def test_an_unreadable_trace_is_a_coverage_gap_retried_after_every_unread_trace(tmp_path):
    asked = []
    broken = {"a"}

    def get(path, params):
        if path == "traces":
            return _page([_trace_row("a", "u"), _trace_row("b", "u"), _trace_row("c", "u")], 1, 1)
        asked.append(params["traceId"])
        if params["traceId"] in broken:
            raise TimeoutError("read timed out")
        return _page([{"type": "TOOL", "startTime": "2026-10-05T15:00:00Z"}], 1, 1)

    budget = traces_read.Budget(history_seconds=2)
    rows, coverage, failures = traces_read.history("s", get, tmp_path, budget, clock=_ticks(1))
    assert failures == []
    assert asked == ["a"]
    assert coverage["unreadable"] == [f"trace a unreadable: TimeoutError: read timed out{_at(get, 5)}"]
    assert coverage["covered"] == 0 and [len(r["observations"]) for r in rows] == [0, 0, 0]
    asked.clear()
    rows, coverage, _ = traces_read.history("s", get, tmp_path, budget, clock=_ticks(1))
    assert asked == ["b"]
    broken.clear()
    asked.clear()
    _, coverage, _ = traces_read.history("s", get, tmp_path, traces_read.Budget())
    assert asked == ["c", "a"]
    assert coverage == {
        "listed": True,
        "unlisted": 0,
        "traces": 3,
        "covered": 3,
        "capped": 0,
        "complete": True,
        "unreadable": [],
    }


def test_a_capped_listing_is_judged_within_its_window_and_counts_the_traces_past_the_cap(tmp_path):
    langfuse = Langfuse([_trace_row("a", "u")], pages=50, items=3430)
    _, coverage, _ = traces_read.history("s", langfuse, tmp_path, traces_read.Budget(trace_pages=1))
    assert coverage == {
        "listed": False,
        "unlisted": 3429,
        "traces": 1,
        "covered": 1,
        "capped": 0,
        "complete": True,
        "unreadable": [],
    }


def _deep(path, params):
    if path == "traces":
        return _page([_trace_row("a", "u")], 1, 1)
    return _page([{"id": f"o{params['page']}", "type": "TOOL", "startTime": "2026-10-05T15:00:00Z"}], params["page"], 9)


def test_a_trace_past_the_observation_page_cap_is_kept_as_capped_and_not_read_again(tmp_path):
    budget = traces_read.Budget(observation_pages=2)
    [row], coverage, _ = traces_read.history("s", _deep, tmp_path, budget)
    assert (row["complete"], len(row["observations"])) == (False, 2)
    assert (coverage["covered"], coverage["capped"], coverage["complete"]) == (0, 1, True)
    asked = []
    traces_read.history("s", lambda path, params: asked.append(path) or _deep(path, params), tmp_path, budget)
    assert asked == ["traces"]


def test_a_trace_stopped_by_the_budget_before_its_page_cap_is_not_capped(tmp_path):
    budget = traces_read.Budget(observation_pages=5, history_seconds=3)
    _, coverage, _ = traces_read.history("s", _deep, tmp_path, budget, clock=_ticks(1))
    assert (coverage["capped"], coverage["complete"]) == (0, False)
    assert list(tmp_path.rglob("*.json")) == []


def test_a_listing_stopped_by_the_budget_counts_what_it_did_not_reach(tmp_path):
    def get(path, params):
        if path == "traces":
            return _page([_trace_row(f"p{params['page']}", "u")], params["page"], 9, items=9)
        return _page([], 1, 1)

    budget = traces_read.Budget(history_seconds=1)
    _, coverage, _ = traces_read.history("s", get, tmp_path, budget, clock=_ticks(1))
    assert (coverage["listed"], coverage["unlisted"], coverage["traces"]) == (False, 8, 1)


@pytest.mark.parametrize("items,unlisted", [(3, 2), (1, 0)])
def test_traces_past_the_cap_count_a_trace_repeated_across_pages_once(tmp_path, items, unlisted):
    def get(path, params):
        if path == "traces":
            return _page([_trace_row("a", "u")], params["page"], 3, items=items)
        return _page([], 1, 1)

    _, coverage, _ = traces_read.history("s", get, tmp_path, traces_read.Budget(trace_pages=2))
    assert (coverage["traces"], coverage["unlisted"]) == (1, unlisted)


def test_a_failed_listing_counts_no_traces_past_a_cap(tmp_path):
    def down(path, params):
        raise TimeoutError("read timed out")

    _, coverage, _ = traces_read.history("s", down, tmp_path, traces_read.Budget())
    assert (coverage["listed"], coverage["unlisted"], coverage["complete"]) == (False, 0, False)


def _legacy(tmp_path, trace_id, entry):
    path = tmp_path / f"{trace_id}.json"
    path.write_text(json.dumps(entry))
    return path


OBSERVED = {"type": "TOOL", "name": "Bash", "start": 1, "end": 2, "turn": 1, "error": False, "tokens": 0}
OTHER = {**OBSERVED, "name": "Read"}


def test_a_whole_cache_entry_written_before_the_deduplicated_flag_is_upgraded_without_a_read(tmp_path):
    path = _legacy(tmp_path, "a", {"updated_at": "u", "whole": True, "observations": [OBSERVED, OTHER]})
    langfuse = Langfuse([_trace_row("a", "u")])
    [row], coverage, _ = traces_read.history("s", langfuse, tmp_path, traces_read.Budget(history_seconds=0))
    assert [c for c in langfuse.calls if c[0] == "observations"] == []
    assert row["observations"] == [OBSERVED, OTHER] and row["complete"] is True
    assert (coverage["covered"], coverage["complete"]) == (1, True)
    assert json.loads(path.read_text()) == {
        "updated_at": "u",
        "whole": True,
        "observations": [OBSERVED, OTHER],
        "deduplicated": True,
    }


def test_an_old_cache_entry_holding_a_repeated_observation_is_read_again(tmp_path):
    path = _legacy(tmp_path, "a", {"updated_at": "u", "whole": True, "observations": [OBSERVED, OTHER, OBSERVED]})
    langfuse = Langfuse([_trace_row("a", "u")])
    [row], _, _ = traces_read.history("s", langfuse, tmp_path, traces_read.Budget())
    assert [c[1]["traceId"] for c in langfuse.calls if c[0] == "observations"] == ["a"]
    assert [o["name"] for o in row["observations"]] == ["a"]
    assert json.loads(path.read_text())["observations"] == row["observations"]


def test_a_failed_cache_entry_written_before_the_flag_keeps_its_failure_and_is_retried(tmp_path):
    _legacy(tmp_path, "a", {"updated_at": "u", "failed": "TimeoutError"})
    langfuse = Langfuse([_trace_row("a", "u")])
    _, coverage, _ = traces_read.history("s", langfuse, tmp_path, traces_read.Budget())
    assert [c[1]["traceId"] for c in langfuse.calls if c[0] == "observations"] == ["a"]
    assert coverage["complete"] is True


def test_a_changed_trace_is_read_again_whatever_its_old_entry_held(tmp_path):
    _legacy(tmp_path, "a", {"updated_at": "old", "whole": True, "observations": [OBSERVED]})
    langfuse = Langfuse([_trace_row("a", "new")])
    [row], _, _ = traces_read.history("s", langfuse, tmp_path, traces_read.Budget())
    assert [c[1]["traceId"] for c in langfuse.calls if c[0] == "observations"] == ["a"]
    assert row["observations"][0]["name"] == "a"


def test_a_cache_entry_without_its_whole_history_is_read_again(tmp_path):
    _legacy(tmp_path, "a", {"updated_at": "u", "whole": False, "observations": [OBSERVED], "deduplicated": True})
    langfuse = Langfuse([_trace_row("a", "u")])
    [row], coverage, _ = traces_read.history("s", langfuse, tmp_path, traces_read.Budget())
    assert [c[1]["traceId"] for c in langfuse.calls if c[0] == "observations"] == ["a"]
    assert row["complete"] is True and coverage["complete"] is True


def test_record_carries_active_bindings_coverage_and_a_persistent_outage_start(tmp_path, monkeypatch):
    agents = [{"name": "s-eng-1", "state": "working", "task": "t1", "started_at": 5}]

    def down(path, params):
        raise ConnectionError("refused")

    first = traces_read.record("s", [], agents, 1000, down, home=tmp_path)
    assert first["reader"]["down_since"] == 1000
    assert first["reader"]["active"] == {"bindings": 1, "read": 0}
    assert first["reader"]["failures"] == [
        f"active read of s-eng-1 failed: ConnectionError: refused{_at(down)}",
        f"trace listing failed: ConnectionError: refused{_at(down)}",
    ]
    assert first["active"][0]["read"] is False
    second = traces_read.record("s", [], agents, 2000, down, home=tmp_path)
    assert second["reader"]["down_since"] == 1000
    healthy = traces_read.record("s", [], agents, 3000, Langfuse([]), home=tmp_path)
    assert healthy["reader"]["down_since"] == 0 and healthy["reader"]["failures"] == []
    assert healthy["reader"]["active"] == {"bindings": 1, "read": 1}


def test_missing_keys_become_a_reader_outage_finding(tmp_path):
    from scripts.doctor import traces

    get = traces_read.client({})
    data = traces_read.record("s", [], [], 1000, get, home=tmp_path)
    kinds = [f.kind for f in traces.findings(data, traces.Limits())]
    assert kinds == ["trace reader unavailable"]


def test_the_client_names_the_missing_keys_and_calls_langfuse_with_auth_params_and_timeout(monkeypatch):
    with pytest.raises(RuntimeError) as refused:
        traces_read.client({})("traces", {})
    assert str(refused.value) == "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set"
    calls = []

    class Response:
        def raise_for_status(self):
            calls.append("checked")

        def json(self):
            return {"data": []}

    monkeypatch.setattr(traces_read.httpx, "get", lambda url, **kw: calls.append((url, kw)) or Response())
    env = {"LANGFUSE_HOST": "https://lf/", "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"}
    assert traces_read.client(env, timeout=3)("traces", {"page": 1}) == {"data": []}
    assert calls == [
        ("https://lf/api/public/traces", {"params": {"page": 1}, "auth": ("pk", "sk"), "timeout": 3}),
        "checked",
    ]


def test_each_request_is_capped_under_half_the_active_read_budget(monkeypatch):
    budget, timeouts = traces_read.Budget(), []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": []}

    monkeypatch.setattr(traces_read.httpx, "get", lambda url, **kw: timeouts.append(kw["timeout"]) or Response())
    traces_read.client({"LANGFUSE_HOST": "https://lf", "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"})(
        "traces", {}
    )
    assert timeouts == [budget.request_seconds]
    assert budget.request_seconds < budget.active_seconds / 2


def _routes(monkeypatch, internal):
    sent, client = [], httpx.Client

    def handler(request):
        sent.append(request)
        if request.url.host == "langfuse.homeofanton.com":
            return httpx.Response(502, text="tunnel error")
        return internal(request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(traces_read.httpx, "Client", lambda: client(transport=transport))
    monkeypatch.setattr(traces_read.httpx, "get", lambda url, **kw: client(transport=transport).get(url, **kw))
    return sent


@pytest.mark.parametrize("host", [None, "https://langfuse.homeofanton.com/"])
def test_reads_reach_langfuse_through_the_internal_ingress_while_the_public_tunnel_answers_502(monkeypatch, host):
    sent = _routes(monkeypatch, lambda request: httpx.Response(200, json={"data": []}))
    env = {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"} | ({"LANGFUSE_HOST": host} if host else {})
    assert traces_read.client(env)("traces", {"page": 1}) == {"data": []}
    [request] = sent
    assert str(request.url) == "https://10.10.30.200/api/public/traces?page=1"
    assert request.headers["Host"] == "langfuse.homeofanton.com"
    assert request.extensions["sni_hostname"] == "langfuse.homeofanton.com"
    assert request.headers["Authorization"] == "Basic cGs6c2s="
    assert request.extensions["timeout"]["read"] == traces_read.Budget.request_seconds


@pytest.mark.parametrize("failure", ["backend 503", "internal timeout"])
def test_a_real_backend_failure_on_the_internal_path_still_raises_the_reader_finding(monkeypatch, tmp_path, failure):
    def internal(request):
        if failure == "internal timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(503, text="langfuse down")

    _routes(monkeypatch, internal)
    get = traces_read.client({"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"})
    data = traces_read.record("s", [], [], 1000, get, home=tmp_path)
    assert [f.kind for f in traces.findings(data, traces.Limits())] == ["trace reader unavailable"]
    raised = {"backend 503": "HTTPStatusError", "internal timeout": "ReadTimeout"}[failure]
    assert data["reader"]["failures"][0].startswith(f"trace listing failed: {raised}")


def test_the_listing_keeps_every_page_and_stops_when_the_budget_is_spent(tmp_path):
    asked = []

    def get(path, params):
        if path == "traces":
            assert params["page"] >= 1 and params["page"] not in asked
            asked.append(params["page"])
            return _page([_trace_row(f"p{params['page']}", "u")], params["page"], 9)
        return _page([], 1, 1)

    rows, coverage, failures = traces_read.history("s", get, tmp_path, traces_read.Budget(trace_pages=2))
    assert [r["id"] for r in rows] == ["p1", "p2"] and failures == []
    asked.clear()
    budget = traces_read.Budget(history_seconds=1)
    rows, coverage, _ = traces_read.history("s", get, tmp_path, budget, clock=_ticks(1))
    assert [r["id"] for r in rows] == ["p1"] and coverage["listed"] is False


def test_record_passes_its_budget_clock_and_cache_to_both_reads(tmp_path):
    agents = [{"name": "s-eng-1", "state": "working", "task": "t1", "started_at": 5}]
    langfuse = Langfuse([_trace_row("a", "u")])
    budget = traces_read.Budget(active_page=3, active_seconds=1, history_seconds=100)
    data = traces_read.record("s", [], agents, 1000, langfuse, home=tmp_path, budget=budget, clock=_ticks(2))
    assert data["reader"]["failures"] == ["active read budget of 1 seconds spent before s-eng-1"]
    assert (tmp_path / "s" / traces_read.CACHE_DIR / "a.json").exists()
    langfuse.calls.clear()
    data = traces_read.record("s", [], agents, 2000, langfuse, home=tmp_path, budget=traces_read.Budget(active_page=3))
    assert [c[1]["limit"] for c in langfuse.calls if c[0] == "observations"] == [3, 1]
    assert data["reader"]["historical"]["covered"] == 1


def test_record_runs_the_history_budget_on_its_own_clock(tmp_path):
    budget = traces_read.Budget(history_seconds=1)
    data = traces_read.record(
        "s", [], [], 1000, Langfuse([_trace_row("a", "u")]), home=tmp_path, budget=budget, clock=_ticks(2)
    )
    assert data["reader"]["historical"]["covered"] == 0


def test_record_reads_active_bindings_and_history_side_by_side(tmp_path, monkeypatch):
    import threading

    met = threading.Barrier(2, timeout=5)
    whole = {"listed": True, "unlisted": 0, "traces": 0, "covered": 0, "complete": True, "unreadable": []}

    def active(slug, agents, get, state, seconds, page, clock):
        met.wait()
        return [], ["active failure"]

    def history(slug, get, cache, budget, clock):
        met.wait()
        return [], whole, ["history failure"]

    monkeypatch.setattr(traces_read.registry, "read", active)
    monkeypatch.setattr(traces_read, "history", history)
    data = traces_read.record("s", [], [], 1000, Langfuse([]), home=tmp_path)
    assert data["reader"]["failures"] == ["active failure", "history failure"]
    assert data["reader"]["historical"] == whole


LOCAL = {
    "generated_bytes": 10,
    "accepted_bytes": 10,
    "oldest_unaccepted": 0,
    "pending": 0,
    "overflow": 0,
    "accepted": 103,
    "accepted_at": 0,
    "exporter_alive": True,
    "requested_at": 0,
}


class Project:
    def __init__(self, name, traces=(), listed=True):
        self.name, self.traces, self.listed = name, list(traces), listed

    def __call__(self, path, params):
        if path == "projects":
            assert params == {}
            if self.name is None:
                raise ConnectionError("refused")
            return {"data": [{"id": "p", "name": self.name}]}
        if path == "traces":
            tags = params["tags"] if isinstance(params["tags"], list) else [params["tags"]]
            rows = [t for t in self.traces if set(tags) <= set(t["tags"])] if self.listed or len(tags) > 1 else []
            return _page(rows if params["page"] == 1 else [], params["page"], 1)
        return _page([], params["page"], 1)


def _working(name, session):
    return {"name": name, "state": "working", "task": "t1", "started_at": 5, "conversation_id": session}


def _tagged(agent, session):
    return {"id": f"tr-{agent}", "sessionId": session, "tags": ["swarm:s", f"agent:{agent}"], "updatedAt": "u"}


HISTORICAL = [{"id": "t0", "claimed_by": "s-eng-0", "state": "done"}]
WORKING = [_working("s-eng-1", "c1"), _working("s-ci-1", "c2")]


def test_an_empty_project_while_exporters_report_accepted_observations_is_a_reader_failure(tmp_path, monkeypatch):
    from scripts.doctor import registry, traces

    monkeypatch.setattr(registry, "progress", lambda session_id, harness: dict(LOCAL))
    data = traces_read.record("s", HISTORICAL, WORKING, 10**9, Project("antoncore"), home=tmp_path)
    assert data["reader"]["failures"] == [
        "Langfuse project antoncore holds no trace tagged swarm:s while the exporters of s-eng-1, s-ci-1 "
        "report accepted exports: the reader's keys may belong to another project"
    ]
    assert [b["read"] for b in data["active"]] == [False, False]
    assert data["reader"]["active"] == {"bindings": 2, "read": 0}
    assert (data["reader"]["historical"]["listed"], data["reader"]["historical"]["complete"]) == (False, False)
    assert [f.kind for f in traces.findings(data, traces.Limits())] == ["trace reader unavailable"]


def test_an_unreadable_project_name_still_names_the_empty_read(tmp_path, monkeypatch):
    from scripts.doctor import registry

    monkeypatch.setattr(registry, "progress", lambda session_id, harness: dict(LOCAL))
    data = traces_read.record("s", [], WORKING[:1], 10**9, Project(None), home=tmp_path)
    assert data["reader"]["failures"][0].startswith(
        "Langfuse project unknown (ConnectionError) holds no trace tagged swarm:s"
    )


def test_an_empty_project_with_no_accepted_observations_is_judged_as_read(tmp_path, monkeypatch):
    from scripts.doctor import registry, traces

    monkeypatch.setattr(registry, "progress", lambda session_id, harness: None)
    data = traces_read.record("s", HISTORICAL, WORKING[:1], 10**9, Project("agent-swarm"), home=tmp_path)
    assert data["reader"]["failures"] == []
    kinds = sorted(f.kind for f in traces.findings(data, traces.Limits()))
    assert kinds == ["telemetry never exported", "untraced session", "untraced session"]


def test_genuine_missing_and_unattributed_traces_stay_detected_in_the_swarm_project(tmp_path, monkeypatch):
    from scripts.doctor import registry, traces

    monkeypatch.setattr(registry, "progress", lambda session_id, harness: dict(LOCAL))
    project = Project("agent-swarm", [_tagged("s-eng-1", "c1")])
    data = traces_read.record("s", HISTORICAL, WORKING, 10**9, project, home=tmp_path)
    assert data["reader"]["failures"] == []
    found = traces.findings(data, traces.Limits())
    assert [f.subject for f in found if f.kind == "untraced session"] == ["s-ci-1", "s-eng-0"]
    assert [f.subject for f in found if f.kind == "telemetry misattributed"] == ["s-ci-1.5.unattributed"]


def test_working_agents_without_traces_in_a_project_that_holds_the_swarm_stay_misattributed(tmp_path, monkeypatch):
    from scripts.doctor import registry, traces

    monkeypatch.setattr(registry, "progress", lambda session_id, harness: dict(LOCAL))
    project = Project("agent-swarm", [_tagged("s-eng-0", "c0")])
    data = traces_read.record("s", HISTORICAL, WORKING, 10**9, project, home=tmp_path)
    assert data["reader"]["failures"] == []
    found = traces.findings(data, traces.Limits())
    assert sorted(f.subject for f in found if f.kind == "telemetry misattributed") == [
        "s-ci-1.5.unattributed",
        "s-eng-1.5.unattributed",
    ]


def test_a_failed_listing_with_accepted_exports_is_one_listing_failure(tmp_path, monkeypatch):
    from scripts.doctor import registry

    monkeypatch.setattr(registry, "progress", lambda session_id, harness: dict(LOCAL))

    def listing_down(path, params):
        if path == "traces" and isinstance(params["tags"], str):
            raise ConnectionError("refused")
        return Project("antoncore")(path, params)

    data = traces_read.record("s", [], WORKING[:1], 10**9, listing_down, home=tmp_path)
    assert data["reader"]["failures"] == [f"trace listing failed: ConnectionError: refused{_at(listing_down, 2)}"]


def test_a_binding_trace_missing_from_an_empty_listing_is_not_an_empty_project(tmp_path, monkeypatch):
    from scripts.doctor import registry

    monkeypatch.setattr(registry, "progress", lambda session_id, harness: dict(LOCAL))
    project = Project("agent-swarm", [_tagged("s-eng-1", "c1")], listed=False)
    data = traces_read.record("s", [], WORKING[:1], 10**9, project, home=tmp_path)
    assert data["reader"]["failures"] == []
    assert data["active"][0]["read"] is True


def test_wait_reads_stop_at_their_budget_and_name_the_agents_left_unread():
    now = [0.0]
    read = []

    def history(name):
        read.append(name)
        now[0] += 4
        return [{"at": 1, "until": 2, "who": name}]

    budget = traces_read.Budget(wait_seconds=8)
    found, unread = traces_read.waits(history, ["a", "b", "c", "d"], 0.0, budget, clock=lambda: now[0])
    assert read == ["a", "b"]
    assert found == {"a": [{"at": 1, "until": 2, "who": "a"}], "b": [{"at": 1, "until": 2, "who": "b"}]}
    assert unread == ["c", "d"]


def test_wait_reads_inside_budget_read_every_agent():
    found, unread = traces_read.waits(lambda name: [name], ["a", "b"], 0.0, traces_read.Budget(), clock=lambda: 0.0)
    assert (found, unread) == ({"a": ["a"], "b": ["b"]}, [])
    assert (traces_read.Budget().wait_seconds, traces_read.Budget().step_seconds) == (15.0, 55.0)


@pytest.mark.parametrize(
    "history_end, expected", [(0.0, ["a", "b"]), (50.0, ["a", "b"]), (51.0, ["a"]), (55.0, []), (60.0, [])]
)
def test_wait_reads_share_the_step_deadline_with_the_trace_reads_before_them(history_end, expected):
    now = [history_end]
    read = []

    def history(name):
        read.append(name)
        now[0] += 4.5
        return []

    budget = traces_read.Budget(wait_seconds=8)
    found, unread = traces_read.waits(history, ["a", "b", "c"], 0.0, budget, clock=lambda: now[0])
    assert read == expected
    assert unread == ["a", "b", "c"][len(expected) :]
