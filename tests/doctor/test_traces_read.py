import pytest

from scripts.doctor import traces_read


def _page(rows, page, total):
    return {"data": rows, "meta": {"page": page, "totalPages": total}}


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
    assert (coverage, failures) == ({"listed": True, "traces": 1, "covered": 1, "complete": True, "unreadable": []}, [])
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
    def __init__(self, traces, pages=1):
        self.traces, self.pages, self.calls = traces, pages, []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "traces":
            assert sum(1 for call in self.calls if call[0] == "traces") <= traces_read.Budget().trace_pages * 3
            return _page(self.traces if params["page"] == 1 else [], params["page"], self.pages)
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
    assert coverage == {"listed": True, "traces": 4, "covered": 1, "complete": False, "unreadable": []}
    assert failures == []
    assert [len(r["observations"]) for r in rows] == [1, 0, 0, 0]
    rows, coverage, _ = traces_read.history("s", langfuse, tmp_path, traces_read.Budget())
    assert coverage["covered"] == 4


def test_the_trace_listing_stops_at_its_page_cap(tmp_path):
    langfuse = Langfuse([_trace_row("a", "u")], pages=50)
    _, coverage, failures = traces_read.history("s", langfuse, tmp_path, traces_read.Budget(trace_pages=3))
    assert [c[1]["page"] for c in langfuse.calls if c[0] == "traces"] == [1, 2, 3]
    assert coverage["listed"] is False and coverage["complete"] is False
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
    assert failures == ["trace listing failed: TimeoutError: read timed out"]


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
    assert coverage["unreadable"] == ["trace a unreadable: TimeoutError: read timed out"]
    assert coverage["covered"] == 0 and [len(r["observations"]) for r in rows] == [0, 0, 0]
    asked.clear()
    rows, coverage, _ = traces_read.history("s", get, tmp_path, budget, clock=_ticks(1))
    assert asked == ["b"]
    broken.clear()
    asked.clear()
    _, coverage, _ = traces_read.history("s", get, tmp_path, traces_read.Budget())
    assert asked == ["c", "a"]
    assert coverage == {"listed": True, "traces": 3, "covered": 3, "complete": True, "unreadable": []}


def test_a_capped_listing_still_backfills_what_it_listed(tmp_path):
    langfuse = Langfuse([_trace_row("a", "u")], pages=50)
    _, coverage, _ = traces_read.history("s", langfuse, tmp_path, traces_read.Budget(trace_pages=1))
    assert coverage == {"listed": False, "traces": 1, "covered": 1, "complete": False, "unreadable": []}


def test_record_carries_active_bindings_coverage_and_a_persistent_outage_start(tmp_path, monkeypatch):
    agents = [{"name": "s-eng-1", "state": "working", "task": "t1", "started_at": 5}]

    def down(path, params):
        raise ConnectionError("refused")

    first = traces_read.record("s", [], agents, 1000, down, home=tmp_path)
    assert first["reader"]["down_since"] == 1000
    assert first["reader"]["active"] == {"bindings": 1, "read": 0}
    assert first["reader"]["failures"] == [
        "active read of s-eng-1 failed: ConnectionError: refused",
        "trace listing failed: ConnectionError: refused",
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
    def __init__(self, name, traces=()):
        self.name, self.traces = name, list(traces)

    def __call__(self, path, params):
        if path == "projects":
            assert params == {}
            if self.name is None:
                raise ConnectionError("refused")
            return {"data": [{"id": "p", "name": self.name}]}
        if path == "traces":
            tags = params["tags"] if isinstance(params["tags"], list) else [params["tags"]]
            rows = [t for t in self.traces if set(tags) <= set(t["tags"])]
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
        "report accepted observations: the reader's keys belong to another project"
    ]
    assert [b["read"] for b in data["active"]] == [False, False]
    assert data["reader"]["active"] == {"bindings": 2, "read": 0}
    assert (data["reader"]["historical"]["listed"], data["reader"]["historical"]["complete"]) == (False, False)
    assert [f.kind for f in traces.findings(data, traces.Limits())] == ["trace reader unavailable"]


def test_an_unreadable_project_name_still_names_the_empty_read(tmp_path, monkeypatch):
    from scripts.doctor import registry

    monkeypatch.setattr(registry, "progress", lambda session_id, harness: dict(LOCAL))
    data = traces_read.record("s", [], WORKING[:1], 10**9, Project(None), home=tmp_path)
    assert data["reader"]["failures"][0].startswith("Langfuse project unknown holds no trace tagged swarm:s")


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
