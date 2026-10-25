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


@pytest.mark.parametrize("total,rows", [(2, [{"id": "one"}]), (2, [{"id": "one"}, {"id": "one"}])])
def test_incomplete_remote_identity_listing_is_unknown(cursors, total, rows):
    from scripts.doctor import observation_reconciliation

    _session(cursors, "sid", [], 0, accepted={"one": "rev"})

    def get(path, params):
        assert path == "observations"
        assert params == {"traceId": "tr", "limit": 100, "page": 1}
        return {"data": rows, "meta": {"totalItems": total}}

    with pytest.raises(ValueError, match="identity listing is incomplete"):
        observation_reconciliation.reconcile(get, "sid", "claude", "tr")


def test_remote_identity_pagination_is_bounded(cursors, monkeypatch):
    from scripts.doctor import observation_reconciliation

    _session(cursors, "sid", [], 0, accepted={"one": "rev"})
    monkeypatch.setattr(observation_reconciliation, "MAX_PAGES", 2)
    pages = []

    def get(path, params):
        assert path == "observations" and params["traceId"] == "tr" and params["limit"] == 100
        pages.append(params["page"])
        assert len(pages) <= 2
        return {"data": [], "meta": {"totalItems": 1000}}

    with pytest.raises(ValueError, match="page budget exhausted"):
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

    with pytest.raises(ValueError, match="accepted"):
        observation_reconciliation.reconcile(get, "sid", "claude", "tr")
