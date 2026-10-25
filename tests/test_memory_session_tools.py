"""Tests for SV2-IDX-05 session tools and citation ready record range expansion."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from hooks.mcp import sessions as mcp_sessions
from hooks.memory import archive
from hooks.memory import search as core
from hooks.memory.api import records
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, Rejected, digest
from tests import sv2_idx05_cases
from tests.sv2_idx05_cases import Bank, body, grant

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parent.parent / "evidence" / "SV2-IDX-05"
GOOD = {"session_uid": "s", "source_generation": "g", "first_sequence": 1, "last_sequence": 2, "event_ids": ["e"]}


@pytest.fixture
def inputs():
    return json.loads(sv2_idx05_cases.FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def bank(inputs, tmp_path):
    return Bank(inputs, tmp_path)


def error(answer):
    return answer["error"]


def cited(bank):
    answer = sv2_idx05_cases.hit(bank)[0]
    return next(item["citation"] for item in answer["results"] if item["kind"] == "tool")


def test_page_slices_and_names_the_next_position():
    assert records._page([1, 2, 3], 0, 2) == ([1, 2], 2)
    assert records._page([1, 2, 3], 2, 2) == ([3], None)
    assert records._page([1, 2], 0, 2) == ([1, 2], None)


def test_citation_spans_the_chunk_evidence():
    chunk = {
        "session_uid": "s",
        "source_generation": "g",
        "evidence": [{"event_id": "e3", "sequence": 3}, {"event_id": "e1", "sequence": 1}],
    }
    assert records.citation(chunk) == {
        "session_uid": "s",
        "source_generation": "g",
        "first_sequence": 1,
        "last_sequence": 3,
        "event_ids": ["e3", "e1"],
    }


def test_grant_digest_ignores_project_order():
    assert records._grant_digest(Grant("p", "c", frozenset({"b", "a"}))) == digest(["p", "c", ["a", "b"]])


@pytest.mark.parametrize(
    "request_body,detail",
    [
        (["x"], "request must be an object"),
        ({"schema_version": "1.0", "access_domain": "d"}, "unsupported schema_version"),
        ({"schema_version": 2, "access_domain": "d"}, "unsupported schema_version"),
        ({"schema_version": SCHEMA_VERSION, "access_domain": "d", "extra": 1}, "request carries unknown fields"),
        ({"schema_version": SCHEMA_VERSION, "access_domain": ""}, "access_domain must be a non-empty string"),
        ({"schema_version": SCHEMA_VERSION, "access_domain": 3}, "access_domain must be a non-empty string"),
    ],
)
def test_envelope_refusals(request_body, detail):
    with pytest.raises(Rejected) as raised:
        records._envelope(request_body, records.LIST_FIELDS)
    assert (raised.value.error_class, raised.value.detail) == ("invalid_request", detail)


def test_envelope_checks_session_uid_only_where_the_tool_takes_it():
    request = {"schema_version": "2.7", "access_domain": "d"}
    assert records._envelope(request, records.LIST_FIELDS) is request
    with pytest.raises(Rejected) as raised:
        records._envelope({**request, "session_uid": ""}, records.GET_FIELDS)
    assert raised.value.detail == "session_uid must be a non-empty string"


@pytest.mark.parametrize("value", [0, 4, True, "2", None])
def test_bounded_refuses_values_outside_the_range(value):
    with pytest.raises(Rejected) as raised:
        records._bounded({"n": value}, "n", 2, 1, 3)
    assert (raised.value.error_class, raised.value.detail) == ("invalid_request", "n must be an integer from 1 to 3")


def test_bounded_takes_the_default_and_the_edges():
    assert records._bounded({}, "n", 2, 1, 3) == 2
    assert records._bounded({"n": 1}, "n", 2, 1, 3) == 1
    assert records._bounded({"n": 3}, "n", 2, 1, 3) == 3


@pytest.mark.parametrize(
    "change,detail",
    [
        ({"extra": 1}, f"citation must carry exactly {sorted(records.CITATION_FIELDS)}"),
        ({"first_sequence": 0}, "citation sequences must be positive and ordered"),
        ({"first_sequence": 3}, "citation sequences must be positive and ordered"),
        ({"first_sequence": True}, "citation sequences must be positive and ordered"),
        ({"last_sequence": "2"}, "citation sequences must be positive and ordered"),
        ({"event_ids": []}, "citation event_ids must be a non-empty list of strings"),
        ({"event_ids": "e"}, "citation event_ids must be a non-empty list of strings"),
        ({"event_ids": [""]}, "citation event_ids must be a non-empty list of strings"),
        ({"session_uid": ""}, "citation session_uid and source_generation must be non-empty strings"),
        ({"source_generation": 1}, "citation session_uid and source_generation must be non-empty strings"),
    ],
)
def test_citation_refusals(change, detail):
    with pytest.raises(Rejected) as raised:
        records._citation({**GOOD, **change})
    assert (raised.value.error_class, raised.value.detail) == ("invalid_request", detail)


def test_a_missing_or_partial_citation_is_refused():
    for value in (None, {"session_uid": "s"}):
        with pytest.raises(Rejected):
            records._citation(value)
    assert records._citation({**GOOD, "first_sequence": 2}) == {**GOOD, "first_sequence": 2}


def test_list_pages_every_visible_session_once(inputs, bank):
    first = bank.ask("session_list", body(inputs, limit=2))
    assert first["state"] == "answered" and first["tool"] == "session_list" and first["total"] == 3
    assert sv2_idx05_cases.listed(bank, first) == ["lock", "kernel"] and first["next_cursor"].startswith("cursor-")
    second = bank.ask("session_list", body(inputs, limit=2, cursor=first["next_cursor"]))
    assert sv2_idx05_cases.listed(bank, second) == ["readme"] and second["next_cursor"] is None
    assert first["operation_id"] == f"session_list:{digest(body(inputs, limit=2))[:16]}"
    assert first["schema_version"] == SCHEMA_VERSION and "alias" not in first


def test_list_defaults_to_twenty_and_narrows_projects(inputs, bank):
    everything = bank.ask("session_list", body(inputs))
    assert everything["total"] == 3 and everything["next_cursor"] is None
    narrowed = bank.ask("session_list", body(inputs, projects=[inputs["project"]]))
    assert sv2_idx05_cases.listed(bank, narrowed) == ["lock", "readme"]
    assert bank.ask("session_list", body(inputs, limit=50))["state"] == "answered"
    assert error(bank.ask("session_list", body(inputs, limit=51)))["detail"] == "limit must be an integer from 1 to 50"


def test_list_hides_sessions_that_are_not_retained(inputs, bank):
    bank.revoke("readme", "restricted")
    assert sv2_idx05_cases.listed(bank, bank.ask("session_list", body(inputs))) == ["lock", "kernel"]


def test_summary_reports_harness_projects_tasks_timestamps_and_sources(inputs, bank):
    session = bank.ask("session_get", body(inputs, session_uid=bank.uid("lock")))["session"]
    generation = inputs["sessions"][0]["source_generation"]
    assert session == {
        "session_uid": bank.uid("lock"),
        "harness": "claude",
        "project_ids": [inputs["project"]],
        "task_ids": ["fixture-lock"],
        "first_timestamp": "2026-10-20T09:00:00Z",
        "last_timestamp": "2026-10-20T09:05:30Z",
        "events": 6,
        "sources": [{"source_generation": generation, "events": 6, "durable_through": 6}],
    }
    kernel = bank.ask("session_get", body(inputs, session_uid=bank.uid("kernel")))["session"]
    assert (kernel["task_ids"], kernel["first_timestamp"], kernel["last_timestamp"]) == ([], None, None)


def test_get_narrowed_to_another_project_is_unavailable(inputs, bank):
    answer = bank.ask("session_get", body(inputs, session_uid=bank.uid("kernel"), projects=[inputs["project"]]))
    assert error(answer) == {"class": "evidence_unavailable", "retry": "never", "detail": records.UNAVAILABLE}
    assert bank.counter(records.RESOLUTION_FAILURES) == 1


@pytest.mark.parametrize(
    "fields",
    [{"access_domain": "personal-corpus"}, {"projects": ["github.com/the-cloud-clockwork/antoncore-private"]}],
)
def test_scope_outside_the_grant_is_refused_and_counted(inputs, bank, fields):
    answer = bank.ask("session_list", {**body(inputs), **fields})
    assert error(answer) == {"class": "forbidden_scope", "retry": "never", "detail": core.SCOPE_REFUSED}
    assert bank.counter(core.SCOPE_REJECTIONS) == 1


@pytest.mark.parametrize("projects", ["x", [""], [3]])
def test_malformed_projects_are_refused(inputs, bank, projects):
    answer = bank.ask("session_list", body(inputs, projects=projects))
    assert error(answer)["detail"] == "projects must be a list of non-empty strings"
    assert bank.counter(core.SCOPE_REJECTIONS) == 0


def test_search_hits_carry_citation_metadata_and_no_resume(inputs, bank):
    answer = bank.ask("session_search", body(inputs, query=inputs["queries"]["hit"]))
    assert answer["tool"] == "session_search" and answer["state"] == "answered" and answer["results"]
    assert answer["mode"] == "lexical_only" and answer["vector"]["reason"] == "no_active_index"
    for result in answer["results"]:
        chunk = bank.tools._chunk(inputs["grant"]["corpus"], result)
        assert result["citation"] == records.citation(chunk) and result["omitted_chars"] == chunk["omitted_chars"]
        assert result["session"] == {
            "harness": "claude",
            "task_ids": ["fixture-lock"],
            "first_timestamp": "2026-10-20T09:00:00Z",
            "last_timestamp": "2026-10-20T09:05:30Z",
        }
        assert not [key for key in result if "resume" in key]


def test_search_refusals_pass_through_with_their_class(inputs, bank):
    refused = bank.ask("session_search", {**body(inputs, query="x"), "access_domain": "personal-corpus"})
    assert error(refused) == {"class": "forbidden_scope", "retry": "never", "detail": core.SCOPE_REFUSED}
    invalid = bank.ask("session_search", body(inputs, query=""))
    assert error(invalid)["detail"] == "query must be a non-empty string"


def test_search_dependency_failure_keeps_its_retry(inputs, bank, monkeypatch):
    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(bank.tools.searcher, "search", locked)
    answer = bank.ask("session_search", body(inputs, query="x"))
    assert error(answer)["class"] == "dependency_unavailable" and error(answer)["retry"] == "same_request"


def test_records_expand_the_cited_range_with_context(inputs, bank):
    citation = cited(bank)
    answer = bank.ask("session_records", body(inputs, citation=citation, before=1, after=1))
    sequences = [item["sequence"] for item in answer["records"]]
    low, high = max(1, citation["first_sequence"] - 1), citation["last_sequence"] + 1
    assert sequences == list(range(low, high + 1)) and answer["total"] == len(sequences)
    assert answer["complete"] is True and answer["truncated"] is False and answer["next_cursor"] is None
    assert [item["event_id"] for item in answer["records"] if item["cited"]] == citation["event_ids"]
    first = answer["records"][0]
    assert sorted(first) == sorted(
        [
            "event_id",
            "sequence",
            "byte_start",
            "byte_end",
            "project_id",
            "cited",
            "kind",
            "timestamp",
            "task_id",
            "normalized_hash",
            "record",
            "source_text",
        ]
    )
    assert first["task_id"] == "fixture-lock" and first["timestamp"].startswith("2026-10-20")
    assert first["kind"] == first["record"]["kind"]
    assert sv2_idx05_cases.authoritative(bank, citation, answer)


def test_records_default_to_the_cited_range_only(inputs, bank):
    citation = cited(bank)
    answer = bank.ask("session_records", body(inputs, citation=citation))
    sequences = [item["sequence"] for item in answer["records"]]
    assert sequences[0] == citation["first_sequence"] and sequences[-1] == citation["last_sequence"]


def test_records_page_with_a_cursor_and_mark_truncation(inputs, bank):
    request = body(inputs, citation=cited(bank), after=1, limit=1)
    head = bank.ask("session_records", request)
    assert head["truncated"] is True and head["complete"] is False and len(head["records"]) == 1
    rest = sv2_idx05_cases.expand(bank, request, head["next_cursor"], inputs["clock"]["now"])
    sequences = [item["sequence"] for item in head["records"]] + [page[0]["sequence"] for page in rest]
    assert sequences == sorted(set(sequences)) and len(sequences) == head["total"]


@pytest.mark.parametrize(
    "field,value,detail",
    [
        ("before", 21, "before must be an integer from 0 to 20"),
        ("after", -1, "after must be an integer from 0 to 20"),
        ("limit", 201, "limit must be an integer from 1 to 200"),
    ],
)
def test_records_bounds(inputs, bank, field, value, detail):
    citation = cited(bank)
    assert error(bank.ask("session_records", body(inputs, citation=citation, **{field: value})))["detail"] == detail
    widest = body(inputs, citation=citation, before=20, after=20, limit=200)
    assert bank.ask("session_records", widest)["state"] == "answered"


def test_records_in_a_foreign_corpus_are_refused_and_counted(inputs, bank):
    request = {**body(inputs, citation=cited(bank)), "access_domain": "personal-corpus"}
    assert error(bank.ask("session_records", request)) == {
        "class": "forbidden_scope",
        "retry": "never",
        "detail": core.SCOPE_REFUSED,
    }
    assert bank.counter(core.SCOPE_REJECTIONS) == 1 and bank.counter(records.RESOLUTION_FAILURES) == 0


@pytest.mark.parametrize(
    "change",
    [{"first_sequence": 3}, {"last_sequence": 9}, {"event_ids": ["event-unknown"]}, {"source_generation": "x"}],
)
def test_a_stale_citation_is_refused_and_counted(inputs, bank, change):
    citation = cited(bank)
    assert (citation["first_sequence"], citation["last_sequence"]) == (2, 3)
    answer = bank.ask("session_records", body(inputs, citation={**citation, **change}))
    assert error(answer) == {"class": "evidence_unavailable", "retry": "never", "detail": records.STALE_CITATION}
    assert bank.counter(records.RESOLUTION_FAILURES) == 1


def test_a_revoked_session_is_unavailable_through_every_name(inputs, bank):
    citation = cited(bank)
    bank.revoke("lock")
    for name in ("session_records", "get_session_segment"):
        answer = bank.ask(name, body(inputs, citation=citation))
        assert error(answer) == {"class": "evidence_unavailable", "retry": "never", "detail": records.UNAVAILABLE}
    assert bank.counter(records.RESOLUTION_FAILURES) == 2


def test_an_archive_read_refusal_counts_as_unavailable(inputs, bank, monkeypatch):
    citation = cited(bank)

    def refuse(*args):
        raise archive.EvidenceRefused("named detail")

    monkeypatch.setattr(archive, "source_range", refuse)
    answer = bank.ask("session_records", body(inputs, citation=citation))
    assert error(answer) == {"class": "evidence_unavailable", "retry": "never", "detail": records.UNAVAILABLE}
    assert bank.counter(records.RESOLUTION_FAILURES) == 1


def test_altered_archive_bytes_are_refused_and_counted(inputs, bank):
    citation = cited(bank)
    for path in (bank.corpus.root / "central/objects").rglob("*"):
        if path.is_file():
            path.write_bytes(b"altered")
    answer = bank.ask("session_records", body(inputs, citation=citation))
    assert error(answer) == {"class": "evidence_integrity", "retry": "never", "detail": archive.ALTERED}
    assert bank.counter(records.RESOLUTION_FAILURES) == 1


def test_a_catalog_failure_is_retryable(inputs, bank, monkeypatch):
    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setitem(records.TOOLS, "session_list", locked)
    answer = bank.ask("session_list", body(inputs))
    assert error(answer) == {"class": "dependency_unavailable", "retry": "same_request", "detail": records.TOOL_FAILED}


def test_cursor_type_and_request_binding(inputs, bank):
    first = bank.ask("session_list", body(inputs, limit=1))
    assert error(bank.ask("session_list", body(inputs, limit=1, cursor=3)))["detail"] == "cursor must be a string"
    other = bank.ask("session_list", body(inputs, limit=2, cursor=first["next_cursor"]))
    assert error(other) == {"class": "invalid_request", "retry": "never", "detail": records.CURSOR_OTHER_REQUEST}
    unknown = bank.ask("session_list", body(inputs, limit=1, cursor="cursor-unknown"))
    assert error(unknown) == {"class": "cursor_expired", "retry": "never", "detail": records.CURSOR_EXPIRED}


def test_a_cursor_from_another_grant_reads_as_expired(inputs, bank):
    first = bank.ask("session_list", body(inputs, limit=1))
    narrow = grant(inputs, narrow=True)
    answer = bank.ask("session_list", body(inputs, limit=1, cursor=first["next_cursor"]), granted=narrow)
    assert error(answer)["detail"] == records.CURSOR_EXPIRED


def test_a_cursor_expires_at_its_lifetime(inputs, bank):
    now = inputs["clock"]["now"]
    cursor = bank.ask("session_list", body(inputs, limit=1), now)["next_cursor"]
    edge = now + records.CURSOR_SECONDS
    assert bank.ask("session_list", body(inputs, limit=1, cursor=cursor), edge - 1)["state"] == "answered"
    late = bank.ask("session_list", body(inputs, limit=1, cursor=cursor), edge)
    assert error(late)["detail"] == records.CURSOR_EXPIRED


def test_a_reissued_cursor_keeps_its_lifetime_and_expired_rows_are_pruned(inputs, bank):
    now = inputs["clock"]["now"]
    cursor = bank.ask("session_list", body(inputs, limit=1), now)["next_cursor"]
    assert bank.ask("session_list", body(inputs, limit=1), now + 600)["next_cursor"] == cursor
    later = now + records.CURSOR_SECONDS + 1
    assert error(bank.ask("session_list", body(inputs, limit=1, cursor=cursor), later))["detail"] == (
        records.CURSOR_EXPIRED
    )
    reissued = bank.ask("session_list", body(inputs, limit=1), now + 10 * records.CURSOR_SECONDS)["next_cursor"]
    assert reissued == cursor
    rows = bank.corpus.catalog.db.execute("SELECT expires_at FROM session_cursor").fetchall()
    assert rows == [(now + 11 * records.CURSOR_SECONDS,)]


def test_a_cursor_survives_a_restart(inputs, bank):
    first = bank.ask("session_list", body(inputs, limit=1))
    page = bank.ask("session_list", body(inputs, limit=1, cursor=first["next_cursor"]))
    bank.restart()
    assert bank.ask("session_list", body(inputs, limit=1, cursor=first["next_cursor"])) == page


def test_a_changed_listing_expires_the_cursor(inputs, bank):
    first = bank.ask("session_list", body(inputs, limit=1))
    bank.revoke("kernel", "restricted")
    answer = bank.ask("session_list", body(inputs, limit=1, cursor=first["next_cursor"]))
    assert error(answer) == {"class": "cursor_expired", "retry": "never", "detail": records.CURSOR_MOVED}


def test_aliases_terminate_in_their_targets_and_are_counted(inputs, bank):
    answer = bank.ask("list_sessions", body(inputs))
    assert answer["tool"] == "session_list" and answer["alias"] == {
        "name": "list_sessions",
        "target": "session_list",
        "owner": records.ALIAS_OWNER,
        "expires": records.ALIAS_EXPIRES,
    }
    assert bank.ask("search_semantic", body(inputs, query="readme"))["tool"] == "session_search"
    assert bank.ask("get_session", body(inputs, session_uid=bank.uid("lock")))["session"]["harness"] == "claude"
    assert bank.counter(records.ALIAS_CALLS) == 3


def test_an_alias_past_its_expiry_is_refused(inputs, bank):
    answer = bank.ask("list_sessions", body(inputs), records.ALIAS_EXPIRES_AT)
    assert error(answer) == {"class": "alias_expired", "retry": "never", "detail": records.ALIAS_RETIRED}
    assert bank.ask("list_sessions", body(inputs), records.ALIAS_EXPIRES_AT - 1)["state"] == "answered"
    assert bank.counter(records.ALIAS_CALLS) == 2


def test_alias_expiry_matches_its_label():
    assert datetime.fromisoformat(records.ALIAS_EXPIRES).timestamp() == records.ALIAS_EXPIRES_AT


def test_an_unknown_tool_is_refused(inputs, bank):
    answer = bank.ask("restore_session", body(inputs))
    assert error(answer) == {"class": "invalid_request", "retry": "never", "detail": records.UNKNOWN_TOOL}
    assert answer["operation_id"] == f"restore_session:{digest(body(inputs))[:16]}"
    assert bank.counter(records.ALIAS_CALLS) == 0


def test_the_counter_reads_zero_before_any_write(bank):
    assert bank.tools.counter("swarm-fixture", records.RESOLUTION_FAILURES) == 0


def test_mcp_tools_are_hidden_without_a_catalog():
    assert sv2_idx05_cases.registered({}) == []


def test_mcp_tools_register_with_a_catalog_and_aliases_on_request(tmp_path):
    path = str(tmp_path / "catalog.sqlite3")
    tools = sv2_idx05_cases.registered({mcp_sessions.CATALOG_ENV: path})
    assert tools == ["session_get", "session_list", "session_records", "session_search"]
    aliased = sv2_idx05_cases.registered({mcp_sessions.CATALOG_ENV: path, mcp_sessions.ALIASES_ENV: "1"})
    assert aliased == sorted(tools + list(records.ALIASES))
    assert sv2_idx05_cases.registered({mcp_sessions.CATALOG_ENV: path, mcp_sessions.ALIASES_ENV: "yes"}) == tools


@pytest.fixture
def served(inputs, tmp_path, monkeypatch):
    built = Bank(inputs, tmp_path)
    monkeypatch.setenv(mcp_sessions.CATALOG_ENV, str(tmp_path / "central/catalog.sqlite3"))
    monkeypatch.setenv(mcp_sessions.CORPUS_ENV, inputs["grant"]["corpus"])
    monkeypatch.setenv(mcp_sessions.PROJECTS_ENV, " , ".join(inputs["grant"]["projects"]) + ",")
    monkeypatch.setenv(mcp_sessions.PRINCIPAL_ENV, inputs["grant"]["principal"])
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "a display name that grants nothing")
    monkeypatch.setenv(mcp_sessions.ALIASES_ENV, "1")
    monkeypatch.delenv(mcp_sessions.OBJECTS_ENV, raising=False)
    monkeypatch.setattr(mcp_sessions, "_tools", None)
    functions = {}

    class Server:
        def tool(self, name=None):
            def register(function):
                functions[name or function.__name__] = function
                return function

            return register

    mcp_sessions.register(Server())
    return built, functions


def test_mcp_grant_comes_from_the_environment(inputs, served):
    assert mcp_sessions.grant() == grant(inputs)


def test_mcp_tools_answer_through_the_records_api(inputs, served):
    built, functions = served
    listed = json.loads(functions["session_list"](limit=2))
    assert listed["state"] == "answered" and listed["total"] == 3
    following = json.loads(functions["session_list"](limit=2, cursor=listed["next_cursor"]))
    assert sv2_idx05_cases.listed(built, following) == ["readme"]
    assert json.loads(functions["session_list"](projects=inputs["project"]))["total"] == 2
    found = json.loads(functions["session_search"](inputs["queries"]["hit"], limit=1, context=0))
    assert len(found["results"]) == 1 and found["results"][0]["context"] == {"before": [], "after": []}
    citation = cited(built)
    narrowed = json.loads(functions["session_search"](inputs["queries"]["readme"], projects=inputs["project"]))
    assert {item["session_uid"] for item in narrowed["results"]} == {built.uid("readme")}
    got = json.loads(functions["session_get"](citation["session_uid"]))
    assert got["session"]["harness"] == "claude"
    expanded = json.loads(functions["session_records"](json.dumps(citation), after=1, limit=1))
    assert len(expanded["records"]) == 1 and expanded["truncated"] is True
    rest = json.loads(
        functions["session_records"](json.dumps(citation), after=1, limit=1, cursor=expanded["next_cursor"])
    )
    assert rest["records"][0]["sequence"] == expanded["records"][0]["sequence"] + 1
    widened = json.loads(functions["session_records"](json.dumps(citation), before=1))
    assert widened["records"][0]["sequence"] == citation["first_sequence"] - 1
    aliased = json.loads(functions["get_session"](json.dumps({"session_uid": citation["session_uid"]})))
    assert aliased["alias"]["name"] == "get_session" and aliased["session"] == got["session"]
    assert json.loads(functions["list_sessions"]())["total"] == 3
    assert functions["get_session_segment"].__doc__ == (
        "Compatibility alias for session_records; takes the session_records request as a JSON object and expires."
    )


def test_mcp_objects_default_beside_the_catalog_and_can_be_named(served, monkeypatch, tmp_path):
    assert mcp_sessions._session_tools().store.root == tmp_path / "central/objects"
    assert mcp_sessions._session_tools() is mcp_sessions._tools
    monkeypatch.setattr(mcp_sessions, "_tools", None)
    monkeypatch.setenv(mcp_sessions.OBJECTS_ENV, str(tmp_path / "elsewhere"))
    assert mcp_sessions._session_tools().store.root == tmp_path / "elsewhere"


def test_mcp_failures_answer_without_raising(served, monkeypatch):
    _, functions = served
    monkeypatch.setattr(mcp_sessions, "_tools", None)
    monkeypatch.delenv(mcp_sessions.CATALOG_ENV)
    logged = []
    monkeypatch.setattr(mcp_sessions, "log", lambda message, fields: logged.append((message, fields)))
    answer = json.loads(functions["session_get"]("s"))
    assert answer == {
        "schema_version": SCHEMA_VERSION,
        "operation_id": "session_get:failed",
        "state": "rejected",
        "error": {"class": "dependency_unavailable", "retry": "same_request", "detail": mcp_sessions.TOOL_FAILED},
    }
    assert logged == [("MCP session_get failed", {"error": f"'{mcp_sessions.CATALOG_ENV}'"})]


@pytest.mark.parametrize("name,argument", [("session_records", "not json"), ("get_session", "not json")])
def test_mcp_malformed_json_answers_without_raising(served, monkeypatch, name, argument):
    _, functions = served
    logged = []
    monkeypatch.setattr(mcp_sessions, "log", lambda message, fields: logged.append(message))
    assert json.loads(functions[name](argument)) == {
        "schema_version": SCHEMA_VERSION,
        "operation_id": f"{name}:failed",
        "state": "rejected",
        "error": {"class": "invalid_request", "retry": "never", "detail": mcp_sessions.BAD_JSON},
    }
    assert logged == []


def test_a_failing_case_keeps_its_reproduction(monkeypatch):
    def failing(inputs, root):
        assert root is None

    monkeypatch.setitem(sv2_idx05_cases.CASES, "c", failing)
    record = sv2_idx05_cases.run_case("c")
    assert record["state"] == "failed" and "observed" not in record
    assert record["reproduce"] == {
        "fixture": "tests/fixtures/swarm_v2/session-tools.json",
        "case": "c",
        "line": failing.__code__.co_firstlineno + 1,
        "assertion": "assert root is None",
    }
    assert record["input_sha256"] == sv2_idx05_cases.run_case("a")["input_sha256"]


def test_a_hit_whose_chunk_is_gone_is_a_stale_citation(inputs, bank):
    result = {"session_uid": bank.uid("lock"), "chunk_id": "chunk-gone"}
    with pytest.raises(records.Unresolved) as raised:
        bank.tools._chunk(inputs["grant"]["corpus"], result)
    assert str(raised.value) == records.STALE_CITATION


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_idx05_cases.run_case(case), sv2_idx05_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)


def test_the_package_result_names_the_case_records_and_states():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, value in sv2_idx05_cases.run_case("a")["input_sha256"].items():
        assert manifest["inputs"][f"tests/fixtures/swarm_v2/{name}"] == value
    assert manifest["cases"] == ["T-SV2-IDX-05-A", "T-SV2-IDX-05-B", "T-SV2-IDX-05-C"]
    assert result["package"] == "SV2-IDX-05"
    assert result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    assert set(result["metrics"]) == {records.RESOLUTION_FAILURES, records.ALIAS_CALLS}
    for case in "abc":
        assert (EVIDENCE.parents[1] / result["cases"][case]).exists()
