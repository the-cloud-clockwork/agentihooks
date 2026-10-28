import json

import pytest

from hooks.observability import agent_trace
from scripts.doctor import registry
from tests.doctor.test_registry import AGENT, Langfuse, _session, _trace, cursors

__all__ = ["cursors"]


@pytest.mark.parametrize("missing", [False, True])
def test_live_reader_reconciles_paged_identities_instead_of_the_inflated_aggregate(cursors, missing):
    transcript = _session(
        cursors,
        "sid",
        ["2026-10-07T08:00:00Z", "2026-10-07T08:00:01Z"],
        1,
        paged={"spans": 9},
        accepted_at="2026-10-07T08:00:10Z",
    )
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    first = agent_trace.session_spans(records[:1], agent_trace.Identity("sid"))
    ids = {f"{span.span_id:016x}" for span in first}
    rows = [{"id": key, "startTime": "2026-10-07T08:00:00Z", "endTime": "2026-10-07T08:00:00Z"} for key in sorted(ids)]
    if missing:
        rows.pop()
    backend = Langfuse([_trace("tr", "sid")], rows, total=len(rows))
    [binding], failures = registry.read("s", [{**AGENT, "conversation_id": "sid"}], backend, {}, 15, 50)
    assert failures == []
    result = binding["remote"]["reconciliation"]
    assert result["accepted"] == 2
    assert result["drift"] == 7
    assert result["missing"] == (sorted(ids - {row["id"] for row in rows}))
    assert len(result["missing"]) == int(missing)


def test_nonpaged_acceptance_uses_ids_and_excludes_legacy_revisions(cursors):
    from scripts.doctor import observation_reconciliation

    _session(cursors, "sid", [], 0, accepted={"accepted": "rev", "old": "legacy"})
    calls = []

    def get(path, params):
        calls.append((path, params))
        return {"data": [{"id": "other"}], "meta": {"totalItems": 1}}

    assert observation_reconciliation.reconcile(get, "sid", "claude", "tr") == {
        "accepted": 1,
        "missing": ["accepted"],
        "drift": 0,
    }
    assert calls == [("observations", {"traceId": "tr", "limit": 100, "page": 1})]


def test_no_accepted_identities_needs_no_remote_read(cursors):
    from scripts.doctor import observation_reconciliation

    def get(path, params):
        raise AssertionError((path, params))

    assert observation_reconciliation.reconcile(get, "sid", "claude", "tr") is None


def test_remote_identity_pages_are_all_read(cursors):
    from scripts.doctor import observation_reconciliation

    _session(cursors, "sid", [], 0, accepted={"last": "rev"})
    calls = []

    def get(path, params):
        assert path == "observations"
        assert params["traceId"] == "tr" and params["limit"] == 100
        calls.append(params["page"])
        if params["page"] == 1:
            return {"data": [{"id": str(i)} for i in range(100)], "meta": {"totalItems": 101}}
        assert params["page"] == 2
        return {"data": [{"id": "last"}], "meta": {"totalItems": 101}}

    assert observation_reconciliation.reconcile(get, "sid", "claude", "tr") == {
        "accepted": 1,
        "missing": [],
        "drift": 0,
    }
    assert calls == [1, 2]


@pytest.mark.parametrize(
    "total,rows,expected,detail",
    [
        (101, [{"id": "one"}], [1, 2, 1, 2, 1, 2], "1 rows with 1 distinct ids against 101 reported"),
        (201, [{"id": "one"}, {"id": "one"}], [1, 1, 1], "2 rows with 1 distinct ids against 201 reported"),
        (2, [{"id": "one"}, {"id": "one"}], [1, 1, 1], "2 rows with 1 distinct ids against 2 reported"),
    ],
)
def test_incomplete_remote_identity_listing_is_unknown(cursors, total, rows, expected, detail):
    from scripts.doctor import observation_reconciliation

    _session(cursors, "sid", [], 0, accepted={"one": "rev"})

    calls = []

    def get(path, params):
        assert path == "observations"
        assert params == {"traceId": "tr", "limit": 100, "page": expected[len(calls)]}
        calls.append(params["page"])
        return {"data": rows, "meta": {"totalItems": total}}

    with pytest.raises(ValueError, match=f"^observation identity listing of trace tr is incomplete: {detail}$"):
        observation_reconciliation.reconcile(get, "sid", "claude", "tr")
    assert calls == expected


@pytest.mark.parametrize("pages,total", [([100, 99], 201), ([42], 103), ([100, 100, 98], 301)])
def test_short_page_before_an_overcounted_total_completes_listing_when_the_next_page_is_empty(cursors, pages, total):
    from scripts.doctor import observation_reconciliation

    rows = [[{"id": f"{page}-{i}"} for i in range(size)] for page, size in enumerate(pages, 1)] + [[]]
    accepted = {row["id"]: "rev" for page in rows for row in page}
    _session(cursors, "sid", [], 0, accepted={**accepted, "lost": "rev"})
    calls = []

    def get(path, params):
        assert path == "observations" and params == {"traceId": "tr", "limit": 100, "page": len(calls) + 1}
        calls.append(params["page"])
        return {"data": rows[params["page"] - 1], "meta": {"totalItems": total}}

    assert observation_reconciliation.reconcile(get, "sid", "claude", "tr") == {
        "accepted": len(accepted) + 1,
        "missing": ["lost"],
        "drift": 0,
    }
    assert calls == list(range(1, len(pages) + 2))


@pytest.mark.parametrize("repeat_page,detail", [(1, "100 rows with 99 distinct"), (2, "101 rows with 100 distinct")])
def test_repeated_remote_identities_are_unknown(cursors, repeat_page, detail):
    from scripts.doctor import observation_reconciliation

    _session(cursors, "sid", [], 0, accepted={"one": "rev"})
    pages = [[{"id": str(i)} for i in range(100)], [{"id": "100"}]]
    pages[repeat_page - 1][-1] = {"id": "0"}

    def get(path, params):
        return {"data": pages[params["page"] - 1], "meta": {"totalItems": 101}}

    with pytest.raises(
        ValueError, match=f"^observation identity listing of trace tr is incomplete: {detail} ids against 101 reported$"
    ):
        observation_reconciliation.reconcile(get, "sid", "claude", "tr")


def test_rows_shifting_between_page_reads_still_yield_the_full_identity_set(cursors):
    from scripts.doctor import observation_reconciliation

    rows = [{"id": str(i)} for i in range(150)]
    _session(cursors, "sid", [], 0, accepted={"149": "rev", "new": "rev"})
    calls = []

    def get(path, params):
        assert path == "observations" and params["traceId"] == "tr" and params["limit"] == 100
        calls.append(params["page"])
        start = (params["page"] - 1) * 100
        body = {"data": rows[start : start + 100], "meta": {"totalItems": len(rows)}}
        if len(calls) == 1:
            rows.insert(0, {"id": "new"})
        return body

    assert observation_reconciliation.reconcile(get, "sid", "claude", "tr") == {
        "accepted": 2,
        "missing": [],
        "drift": 0,
    }
    assert calls == [1, 2, 1, 2]


@pytest.mark.parametrize(
    "pages,total",
    [([41], 44), ([99], 100), ([100, 100, 8], 209), ([100, 19], 123), ([100, 0], 104)],
)
def test_final_short_page_completes_listing_when_langfuse_total_exceeds_rows(cursors, pages, total):
    from scripts.doctor import observation_reconciliation

    rows = [[{"id": f"{page}-{i}"} for i in range(size)] for page, size in enumerate(pages, 1)]
    accepted = {row["id"]: "rev" for page in rows for row in page}
    _session(cursors, "sid", [], 0, accepted={**accepted, "lost": "rev"})
    calls = []

    def get(path, params):
        assert path == "observations" and params["traceId"] == "tr" and params["limit"] == 100
        calls.append(params["page"])
        return {"data": rows[params["page"] - 1], "meta": {"totalItems": total}}

    assert observation_reconciliation.reconcile(get, "sid", "claude", "tr") == {
        "accepted": len(accepted) + 1,
        "missing": ["lost"],
        "drift": 0,
    }
    assert calls == list(range(1, len(pages) + 1))


def test_remote_identity_pagination_is_bounded(cursors, monkeypatch):
    from scripts.doctor import observation_reconciliation

    _session(cursors, "sid", [], 0, accepted={"one": "rev"})
    monkeypatch.setattr(observation_reconciliation, "MAX_PAGES", 2)
    pages = []

    def get(path, params):
        assert path == "observations" and params["traceId"] == "tr" and params["limit"] == 100
        pages.append(params["page"])
        assert len(pages) <= 2
        return {"data": [{"id": f"{params['page']}-{i}"} for i in range(100)], "meta": {"totalItems": 1000}}

    with pytest.raises(ValueError, match="^observation identity page budget exhausted$"):
        observation_reconciliation.reconcile(get, "sid", "claude", "tr")
    assert pages == [1, 2]


@pytest.mark.parametrize("end", [0, 1, 10000])
def test_unavailable_accepted_source_boundary_is_unknown(cursors, end):
    from scripts.doctor import observation_reconciliation

    _session(cursors, "sid", ["2026-10-07T08:00:00Z"], 1, paged={"spans": 9})
    path = agent_trace._cursor_path("sid")
    cursor = json.loads(path.read_text())
    cursor["source"]["accepted_bytes"] = end
    path.write_text(json.dumps(cursor))

    def get(path, params):
        raise AssertionError((path, params))

    message = (
        "accepted observation identities unavailable for paged cursor"
        if end == 0
        else "accepted transcript boundary is unavailable"
    )
    with pytest.raises(ValueError, match=f"^{message}$"):
        observation_reconciliation.reconcile(get, "sid", "claude", "tr")


def test_replaced_native_records_do_not_invent_accepted_tool_identities(cursors):
    from scripts.doctor import observation_reconciliation

    records = [
        {"type": "user", "uuid": "prompt", "timestamp": "2026-10-07T08:00:00Z", "message": {"content": "run"}},
        {
            "type": "assistant",
            "uuid": "reply",
            "timestamp": "2026-10-07T08:00:01Z",
            "message": {"id": "generation", "content": [{"type": "tool_use", "id": "old-tool", "name": "old"}]},
        },
        {
            "type": "assistant",
            "uuid": "reply",
            "timestamp": "2026-10-07T08:00:02Z",
            "message": {"id": "generation", "content": [{"type": "tool_use", "id": "new-tool", "name": "new"}]},
        },
    ]
    transcript = _session(cursors, "sid", [], 0, paged={"spans": 4})
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    cursor_path = agent_trace._cursor_path("sid")
    cursor = json.loads(cursor_path.read_text())
    cursor["source"]["accepted_bytes"] = transcript.stat().st_size
    cursor_path.write_text(json.dumps(cursor))
    accepted = agent_trace.session_spans([records[0], records[2]], agent_trace.Identity("sid"))
    rows = [{"id": f"{span.span_id:016x}"} for span in accepted]

    def get(path, params):
        assert path == "observations"
        assert params == {"traceId": "tr", "limit": 100, "page": 1}
        return {"data": rows, "meta": {"totalItems": len(rows)}}

    result = observation_reconciliation.reconcile(get, "sid", "claude", "tr")
    assert result == {"accepted": 4, "missing": [], "drift": 0}


@pytest.mark.parametrize("paged", [False, True])
def test_active_read_cancellation_stops_identity_work(cursors, paged):
    from scripts.doctor import observation_reconciliation

    _session(cursors, "sid", ["2026-10-07T08:00:00Z"], 1, accepted={"one": "rev"}, paged={"spans": int(paged)})

    def get(path, params):
        raise AssertionError((path, params))

    message = "accepted identity reconstruction cancelled" if paged else "observation identity read cancelled"
    with pytest.raises(TimeoutError, match=f"^{message}$"):
        observation_reconciliation.reconcile(get, "sid", "claude", "tr", lambda: True)


def test_unreadable_accepted_record_is_unknown(cursors):
    from scripts.doctor import observation_reconciliation

    transcript = _session(cursors, "sid", [], 0, paged={"spans": 9})
    transcript.write_text("broken\n")
    path = agent_trace._cursor_path("sid")
    cursor = json.loads(path.read_text())
    cursor["source"]["accepted_bytes"] = transcript.stat().st_size
    path.write_text(json.dumps(cursor))

    def get(path, params):
        raise AssertionError((path, params))

    with pytest.raises(ValueError, match="^accepted transcript contains an unreadable record$"):
        observation_reconciliation.reconcile(get, "sid", "claude", "tr")


def test_missing_paged_transcript_is_unknown(cursors, monkeypatch):
    from hooks.observability import trace_flush
    from scripts.doctor import observation_reconciliation

    _session(cursors, "sid", ["2026-10-07T08:00:00Z"], 1, paged={"spans": 9})
    monkeypatch.setattr(trace_flush, "_transcript", lambda session, request: None)

    def get(path, params):
        raise AssertionError((path, params))

    with pytest.raises(ValueError, match="^accepted observation identities unavailable for paged cursor$"):
        observation_reconciliation.reconcile(get, "sid", "claude", "tr")


def test_paged_source_without_an_acceptance_boundary_is_unknown(cursors):
    from scripts.doctor import observation_reconciliation

    _session(cursors, "sid", ["2026-10-07T08:00:00Z"], 1, paged={"spans": 9})
    path = agent_trace._cursor_path("sid")
    cursor = json.loads(path.read_text())
    cursor.pop("source")
    path.write_text(json.dumps(cursor))

    def get(path, params):
        raise AssertionError((path, params))

    with pytest.raises(ValueError, match="^accepted observation identities unavailable for paged cursor$"):
        observation_reconciliation.reconcile(get, "sid", "claude", "tr")


def test_transcript_fallback_receives_the_session_and_native_harness(cursors, monkeypatch):
    from hooks.observability import trace_flush
    from scripts.doctor import observation_reconciliation

    transcript = _session(cursors, "sid", ["2026-10-07T08:00:00Z"], 1, paged={"spans": 2})
    trace_flush.request_path("sid").unlink()

    def resolve(session, request):
        assert session == "sid"
        assert request == {"target": "codex"}
        return str(transcript)

    monkeypatch.setattr(trace_flush, "_transcript", resolve)
    records = agent_trace.read_entries(str(transcript))
    rows = [{"id": f"{span.span_id:016x}"} for span in agent_trace.session_spans(records, agent_trace.Identity("sid"))]

    def get(path, params):
        assert path == "observations"
        assert params == {"traceId": "tr", "limit": 100, "page": 1}
        return {"data": rows, "meta": {"totalItems": len(rows)}}

    assert observation_reconciliation.reconcile(get, "sid", "codex", "tr") == {"accepted": 2, "missing": [], "drift": 0}


def test_paged_legacy_ids_are_excluded_but_current_accepted_ids_are_retained(cursors):
    from scripts.doctor import observation_reconciliation

    transcript = _session(cursors, "sid", ["2026-10-07T08:00:00Z"], 1, paged={"spans": 2})
    spans = agent_trace.session_spans(agent_trace.read_entries(str(transcript)), agent_trace.Identity("sid"))
    ids = [f"{span.span_id:016x}" for span in spans]
    path = agent_trace._cursor_path("sid")
    cursor = json.loads(path.read_text())
    cursor["accepted"] = {ids[0]: "legacy", ids[1]: "revision", "extra": "revision"}
    path.write_text(json.dumps(cursor))

    def get(path, params):
        assert path == "observations"
        assert params == {"traceId": "tr", "limit": 100, "page": 1}
        return {"data": [{"id": ids[1]}, {"id": "extra"}], "meta": {"totalItems": 2}}

    assert observation_reconciliation.reconcile(get, "sid", "claude", "tr") == {
        "accepted": 2,
        "missing": [],
        "drift": 2,
    }


def test_an_exact_page_of_remote_identities_ends_without_an_extra_read(cursors):
    from scripts.doctor import observation_reconciliation

    _session(cursors, "sid", [], 0, accepted={"99": "rev"})

    def get(path, params):
        assert path == "observations"
        assert params == {"traceId": "tr", "limit": 100, "page": 1}
        return {"data": [{"id": str(i)} for i in range(100)], "meta": {"totalItems": 100}}

    assert observation_reconciliation.reconcile(get, "sid", "claude", "tr") == {
        "accepted": 1,
        "missing": [],
        "drift": 0,
    }


@pytest.mark.parametrize("conversation", ["sid", ""])
def test_registry_passes_the_joined_session_harness_trace_and_cancellation(cursors, monkeypatch, conversation):
    from scripts.doctor import observation_reconciliation

    backend = Langfuse([_trace("tr", "sid")], [], total=0)
    checks = []

    def reconcile(get, session, harness, trace, cancelled, listed):
        assert listed(lambda: {"missing"}) == {"missing"}
        assert get is backend
        assert session == "sid"
        assert harness == "codex"
        assert trace == "tr"
        assert callable(cancelled) and not cancelled()
        checks.append(session)
        return {"accepted": 1, "missing": ["missing"], "drift": 0}

    monkeypatch.setattr(observation_reconciliation, "reconcile", reconcile)
    [binding], failures = registry.read(
        "s", [{**AGENT, "conversation_id": conversation, "harness": "codex"}], backend, {}, 15, 50
    )
    assert failures == []
    assert checks == ["sid"]
    assert binding["remote"]["reconciliation"] == {"accepted": 1, "missing": ["missing"], "drift": 0}


def test_an_unchanged_trace_reuses_its_identity_listing_and_a_changed_one_lists_again(cursors):
    transcript = _session(
        cursors,
        "sid",
        ["2026-10-07T08:00:00Z", "2026-10-07T08:00:01Z"],
        1,
        paged={"spans": 9},
        accepted_at="2026-10-07T08:00:10Z",
    )
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    ids = sorted(f"{span.span_id:016x}" for span in agent_trace.session_spans(records[:1], agent_trace.Identity("sid")))
    rows = [{"id": key, "startTime": "2026-10-07T08:00:00Z", "endTime": "2026-10-07T08:00:00Z"} for key in ids[1:]]
    backend = Langfuse([_trace("tr", "sid")], rows, total=len(rows))
    agent, state = {**AGENT, "conversation_id": "sid"}, {}

    def listings():
        return sum(1 for path, params in backend.calls if path == "observations" and params["limit"] == 100)

    [first], _ = registry.read("s", [agent], backend, state, 15, 50)
    assert listings() == 1
    registry.save(cursors / "state.json", state)
    state = registry.load(cursors / "state.json")
    [again], failures = registry.read("s", [agent], backend, state, 15, 50)
    assert failures == [] and listings() == 1
    assert again["remote"]["reconciliation"] == first["remote"]["reconciliation"]
    assert again["remote"]["reconciliation"]["missing"] == ids[:1]
    backend.observations = [{"id": ids[0], "startTime": "2026-10-07T08:00:05Z", "endTime": None}, *rows]
    backend.total = len(backend.observations)
    [changed], _ = registry.read("s", [agent], backend, state, 15, 50)
    assert listings() == 2
    assert changed["remote"]["reconciliation"]["missing"] == []


def test_a_trace_whose_count_moved_without_a_newer_event_lists_again(cursors):
    _session(cursors, "sid", [], 0, accepted={"a": "rev"})
    backend = Langfuse([_trace("tr", "sid")], [{"id": "a", "startTime": "2026-10-07T08:00:00Z"}], total=1)
    agent, state = {**AGENT, "conversation_id": "sid"}, {}
    registry.read("s", [agent], backend, state, 15, 50)
    backend.total = 2
    registry.read("s", [agent], backend, state, 15, 50)
    assert sum(1 for path, params in backend.calls if params.get("limit") == 100) == 2


def test_a_trace_with_a_newer_event_and_the_same_count_lists_again(cursors):
    _session(cursors, "sid", [], 0, accepted={"a": "rev"})
    row = {"id": "a", "startTime": "2026-10-07T08:00:00Z"}
    backend = Langfuse([_trace("tr", "sid")], [row], total=1)
    agent, state = {**AGENT, "conversation_id": "sid"}, {}
    registry.read("s", [agent], backend, state, 15, 50)
    backend.observations = [{**row, "endTime": "2026-10-07T08:00:05Z"}]
    registry.read("s", [agent], backend, state, 15, 50)
    assert sum(1 for path, params in backend.calls if params.get("limit") == 100) == 2
