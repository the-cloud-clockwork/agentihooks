"""Tests for SV2-IDX-04 scoped exact and semantic evidence search."""

import json
import sqlite3
from pathlib import Path

import pytest

from hooks.memory import search
from hooks.memory.api import search as api
from hooks.memory.api.ingest import SCHEMA_VERSION, Rejected, digest
from hooks.memory.catalog import Catalog
from hooks.memory.embeddings import EmbeddingModel
from hooks.memory.search import Query, ScopeRefused, SessionSearch
from tests import sv2_idx04_cases
from tests.sv2_idx02_cases import Session
from tests.sv2_idx04_cases import Corpus, embedder, grant, request, session_inputs

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parent.parent / "evidence" / "SV2-IDX-04"
DOMAIN = "swarm-fixture"


@pytest.fixture
def inputs():
    return json.loads(sv2_idx04_cases.FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def corpus(inputs, tmp_path):
    built = Corpus(inputs, tmp_path)
    built.index()
    return built


def fake(chunk_id, session="s", preview="text"):
    return {
        "chunk_id": chunk_id,
        "session_uid": session,
        "turn_id": f"turn-{chunk_id}",
        "kind": "turn",
        "project_ids": ["p"],
        "preview": preview,
        "truncated": False,
        "access_domain": DOMAIN,
        "source_generation": "g",
        "evidence": [{"event_id": f"e-{chunk_id}", "sequence": 1, "byte_start": 0, "byte_end": 4}],
    }


def query(inputs, text=None, **fields):
    return Query(DOMAIN, text or inputs["queries"]["recall"], **fields)


def labelled(corpus, answer):
    return sv2_idx04_cases.named(corpus.labels(), answer)


def test_tokens_keep_identifiers_and_drop_edge_punctuation():
    text = 'Set PRAGMA busy_timeout=5000 in Catalog.__init__. "x" sqlite3.OperationalError: ok'
    assert search.tokens(text) == [
        "set",
        "pragma",
        "busy_timeout",
        "5000",
        "in",
        "catalog.__init__",
        "x",
        "sqlite3.operationalerror",
        "ok",
    ]
    assert search.tokens(". : .:") == []


def test_parse_splits_phrases_from_terms_and_drops_stopwords():
    assert search.parse('"Database is Locked" why did the ingest fail ingest "" " "') == (
        ["database is locked"],
        ["fail", "ingest"],
    )
    assert search.parse("why is the") == ([], [])


def test_lexical_requires_every_phrase_and_matches_exact_tokens():
    chunks = [
        fake("c1", preview="database is locked here"),
        fake("c2", preview="the database is fine"),
        fake("c3", preview="Locked database IS LOCKED twice"),
        fake("c4", preview="nothing"),
    ]
    assert search.lexical(chunks, ["database is locked"], ["fine"]) == [
        ("c1", 1.098612, ["database is locked"]),
        ("c3", 1.098612, ["database is locked"]),
    ]
    symbol = [fake("s1", preview="ChunkIndex.build ok"), fake("s2", preview="build it")]
    assert search.lexical(symbol, [], ["chunkindex.build"]) == [("s1", 1.098612, ["chunkindex.build"])]
    assert search.lexical(symbol, [], ["build"]) == [("s2", 1.098612, ["build"])]


def test_lexical_weights_rare_terms_higher():
    chunks = [fake("a", preview="alpha beta"), fake("b", preview="beta"), fake("c", preview="gamma")]
    assert search.lexical(chunks, [], ["alpha", "beta"]) == [
        ("a", 2.302585, ["alpha", "beta"]),
        ("b", 0.916291, ["beta"]),
    ]
    assert search.lexical(chunks, [], []) == []


def test_cosine():
    assert search.cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert search.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert search.cosine([1.0, 2.0], [2.0, 1.0]) == pytest.approx(0.8)


def test_fuse_explains_both_ranks_and_orders_by_fused_then_id():
    fused = search.fuse([("a", 2.0, ["x"]), ("b", 1.0, ["x"])], [("b", 0.9), ("c", 0.5)])
    assert fused == [
        (
            "b",
            {
                "lexical": {"rank": 2, "score": 1.0, "matched": ["x"]},
                "vector": {"rank": 1, "score": 0.9},
                "fused": 0.032522,
            },
        ),
        ("a", {"lexical": {"rank": 1, "score": 2.0, "matched": ["x"]}, "vector": None, "fused": 0.016393}),
        ("c", {"lexical": None, "vector": {"rank": 2, "score": 0.5}, "fused": 0.016129}),
    ]
    assert [item[0] for item in search.fuse([("z", 1.0, ["x"])], [("y", 0.5)])] == ["y", "z"]


def test_relevance_reports_recall_and_reciprocal_rank():
    results = [{"chunk_id": "a"}, {"chunk_id": "b"}, {"chunk_id": "c"}]
    assert search.relevance(results, ["b", "x"]) == {"recall": 0.5, "reciprocal_rank": 0.5}
    assert search.relevance(results, ["z"]) == {"recall": 0.0, "reciprocal_rank": 0.0}
    assert search.relevance(results, ["c", "a"]) == {"recall": 1.0, "reciprocal_rank": 1.0}


def test_grouped_keeps_first_appearance_and_its_score():
    results = [
        {"chunk_id": "a", "session_uid": "s1", "explain": {"fused": 0.3}},
        {"chunk_id": "b", "session_uid": "s2", "explain": {"fused": 0.2}},
        {"chunk_id": "c", "session_uid": "s1", "explain": {"fused": 0.1}},
    ]
    assert search.grouped(results) == [
        {"session_uid": "s1", "best_fused": 0.3, "chunk_ids": ["a", "c"]},
        {"session_uid": "s2", "best_fused": 0.2, "chunk_ids": ["b"]},
    ]


def test_results_carry_fields_locator_and_bounded_context():
    chunks = [fake("a"), fake("b"), fake("c"), fake("d"), fake("x", session="t")]
    explain = {"fused": 0.1}
    [middle] = search._results(chunks, [("b", explain)], 1)
    assert {key: middle[key] for key in search.RESULT_KEYS} == {key: chunks[1][key] for key in search.RESULT_KEYS}
    assert middle["explain"] == explain
    assert middle["locator"] == [
        {"access_domain": DOMAIN, "session_uid": "s", "source_generation": "g", "byte_start": 0, "byte_end": 4}
    ]
    assert [item["chunk_id"] for item in middle["context"]["before"]] == ["a"]
    assert [item["chunk_id"] for item in middle["context"]["after"]] == ["c"]
    assert middle["context"]["before"][0] == {
        "chunk_id": "a",
        "kind": "turn",
        "preview": "text",
        "locator": middle["locator"],
    }
    [first] = search._results(chunks, [("a", explain)], 2)
    assert first["context"]["before"] == [] and [item["chunk_id"] for item in first["context"]["after"]] == ["b", "c"]
    [last] = search._results(chunks, [("x", explain)], 3)
    assert last["context"] == {"before": [], "after": []}
    [none] = search._results(chunks, [("c", explain)], 0)
    assert none["context"] == {"before": [], "after": []}


def test_a_scope_refusal_counts_against_the_grant_corpus(inputs, corpus):
    granted = grant(inputs)
    with pytest.raises(ScopeRefused, match=f"^{search.SCOPE_REFUSED}$"):
        corpus.searcher.search(granted, Query(inputs["foreign_corpus"], "x"))
    with pytest.raises(ScopeRefused):
        corpus.searcher.search(granted, Query(DOMAIN, "x", (inputs["unauthorized_project"],)))
    assert corpus.searcher.scope_rejections_total(DOMAIN) == 2
    assert corpus.searcher.scope_rejections_total(inputs["foreign_corpus"]) == 0
    corpus.searcher.search(granted, Query(DOMAIN, "x", (inputs["project"],)))
    assert corpus.searcher.scope_rejections_total(DOMAIN) == 2


def test_no_rejection_reads_zero(tmp_path):
    assert SessionSearch(Catalog(tmp_path / "c.sqlite3")).scope_rejections_total(DOMAIN) == 0


def test_projects_narrow_inside_retrieval(inputs, corpus):
    narrowed = corpus.searcher.search(grant(inputs), query(inputs, projects=(inputs["project"],)), embedder(inputs))
    wide = corpus.searcher.search(grant(inputs), query(inputs), embedder(inputs))
    assert not any(label.startswith("kernel:") for label in labelled(corpus, narrowed))
    assert any(label.startswith("kernel:") for label in labelled(corpus, wide))


def test_a_session_not_retained_is_not_searched(inputs, corpus):
    uid = inputs["sessions"][2]["session_uid"]
    revision = corpus.catalog.session(DOMAIN, uid)["revision"]
    corpus.catalog.set_state(DOMAIN, uid, "restricted", revision)
    wide = corpus.searcher.search(grant(inputs), query(inputs), embedder(inputs))
    assert not any(label.startswith("kernel:") for label in labelled(corpus, wide))


def test_a_session_without_an_active_chunk_generation_is_not_searched(inputs, corpus, tmp_path):
    item = {**inputs["sessions"][1], "session_uid": "swarm-fixture:claude:worker-a:unactivated"}
    extra = Session(session_inputs(inputs, item), tmp_path)
    extra.append(item["turns"])
    extra.build()
    extra.validate()
    answer = corpus.searcher.search(grant(inputs), query(inputs, inputs["queries"]["symbol"]), embedder(inputs))
    assert {result["session_uid"] for result in answer["results"]} == {inputs["sessions"][1]["session_uid"]}


@pytest.mark.parametrize("limit,context", [(0, 1), (51, 1), (10, -1), (10, 4)])
def test_bounds_are_refused(inputs, corpus, limit, context):
    with pytest.raises(ValueError, match=f"^{search.BAD_BOUNDS}$"):
        corpus.searcher.search(grant(inputs), query(inputs, limit=limit, context=context))
    assert corpus.searcher.scope_rejections_total(DOMAIN) == 0


def test_the_largest_bounds_are_accepted(inputs, corpus):
    answer = corpus.searcher.search(grant(inputs), query(inputs, limit=50, context=3))
    assert answer["results"]


def test_the_answer_shape_and_session_grouping(inputs, corpus):
    plain = corpus.searcher.search(grant(inputs), query(inputs, projects=(inputs["project"],)), embedder(inputs))
    assert set(plain) == {"mode", "vector", "fusion", "query", "results"}
    assert plain["mode"] == "hybrid"
    assert plain["vector"] == {
        "state": "used",
        "index_generation": inputs["index_generation"],
        "model": inputs["model"]["model"],
    }
    assert plain["fusion"] == {"method": "reciprocal_rank", "k": 60}
    assert plain["query"] == {"phrases": ["database is locked"], "terms": ["fail", "ingest"]}
    grouped = corpus.searcher.search(
        grant(inputs), query(inputs, projects=(inputs["project"],), group_sessions=True), embedder(inputs)
    )
    assert grouped["sessions"] == search.grouped(grouped["results"])
    assert {key: value for key, value in grouped.items() if key != "sessions"} == plain


def test_hybrid_recall_finds_the_error_and_the_related_fix(inputs, corpus):
    answer = corpus.searcher.search(grant(inputs), query(inputs, projects=(inputs["project"],)), embedder(inputs))
    found = labelled(corpus, answer)
    assert found[:2] == ["lock:0:turn", "lock:0:tool"] or found[:2] == ["lock:0:tool", "lock:0:turn"]
    assert "lock:1:turn" in found and "lock:1:tool" not in found and "lock:2:turn" not in found
    fix = answer["results"][found.index("lock:1:turn")]
    assert fix["explain"]["lexical"] is None and fix["explain"]["vector"]["rank"] >= 1


def test_the_exact_symbol_ranks_first(inputs, corpus):
    answer = corpus.searcher.search(grant(inputs), query(inputs, inputs["queries"]["symbol"]), embedder(inputs))
    assert labelled(corpus, answer)[0] == "symbol:0:turn"
    assert answer["results"][0]["explain"]["lexical"]["matched"] == ["chunkindex.build"]


def test_the_limit_truncates_results(inputs, corpus):
    answer = corpus.searcher.search(grant(inputs), query(inputs, limit=1), embedder(inputs))
    assert len(answer["results"]) == 1


def test_candidates_are_capped_before_fusion(inputs, corpus, monkeypatch):
    chunks = [fake(f"c{index}") for index in range(10)]
    monkeypatch.setattr(corpus.searcher, "_scoped", lambda domain, projects: chunks)
    monkeypatch.setattr(search, "lexical", lambda c, p, t: [(f"c{index}", 10.0 - index, ["t"]) for index in range(10)])
    monkeypatch.setattr(corpus.searcher, "_vector", lambda q, e, c: ([("c5", 0.9)], {"state": "used"}))
    one = corpus.searcher.search(grant(inputs), query(inputs, limit=1))
    assert one["results"][0]["chunk_id"] == "c0"
    two = corpus.searcher.search(grant(inputs), query(inputs, limit=2))
    assert [item["chunk_id"] for item in two["results"]] == ["c5", "c0"]


def test_vector_candidates_are_capped_and_ordered(inputs, corpus):
    chunks = corpus.searcher._scoped(DOMAIN, sorted(grant(inputs).projects))
    text = "database fix build readme"
    ranked, label = corpus.searcher._vector(Query(DOMAIN, text, limit=1), embedder(inputs), chunks)
    assert len(ranked) == 4 and label["state"] == "used"
    full, _ = corpus.searcher._vector(Query(DOMAIN, text, limit=10), embedder(inputs), chunks)
    assert len(full) == len(chunks)
    assert full == sorted(full, key=lambda item: (-item[1], item[0])) and all(score > 0 for _, score in full)
    assert ranked == full[:4]


def test_a_stale_or_orthogonal_vector_is_skipped(inputs, corpus):
    chunks = corpus.searcher._scoped(DOMAIN, sorted(grant(inputs).projects))
    labels = corpus.labels()
    target = labels["lock:1:turn"]
    corpus.catalog.db.execute("UPDATE chunk_vector SET content_hash = 'stale' WHERE chunk_id = ?", (target,))
    ranked, _ = corpus.searcher._vector(query(inputs), embedder(inputs), chunks)
    ids = [chunk_id for chunk_id, _ in ranked]
    assert target not in ids and labels["lock:2:turn"] not in ids and labels["lock:0:turn"] in ids


class Broken:
    def __init__(self, model, answer):
        self.model, self.answer = model, answer

    def embed(self, texts):
        return self.answer


@pytest.mark.parametrize(
    "provider,reason",
    [
        (None, "no_embedder"),
        ("next", "model_mismatch"),
        ("down", "provider_unavailable"),
        ("short", "provider_answer_invalid"),
    ],
)
def test_vector_unavailability_answers_lexical_only_with_its_reason(inputs, corpus, provider, reason):
    providers = {
        None: None,
        "next": embedder(inputs, "next_model"),
        "down": embedder(inputs, down=True),
        "short": Broken(EmbeddingModel(**inputs["model"]), [[1.0]]),
    }
    answer = corpus.searcher.search(grant(inputs), query(inputs, projects=(inputs["project"],)), providers[provider])
    assert answer["mode"] == "lexical_only"
    assert answer["vector"] == {"state": "unavailable", "reason": reason}
    assert sorted(labelled(corpus, answer)) == ["lock:0:tool", "lock:0:turn"]


def test_no_active_index_answers_lexical_only(inputs, tmp_path):
    corpus = Corpus(inputs, tmp_path)
    answer = corpus.searcher.search(grant(inputs), query(inputs), embedder(inputs))
    assert answer["vector"] == {"state": "unavailable", "reason": "no_active_index"}
    assert answer["mode"] == "lexical_only" and answer["results"]


def test_check_builds_a_query_with_defaults(inputs):
    assert api._check(request(inputs, "find")) == Query(DOMAIN, "find", (), 10, 1, False)
    body = request(inputs, "find", projects=["p"], limit=3, context=0, group_sessions=True)
    assert api._check({**body, "schema_version": "2.5"}) == Query(DOMAIN, "find", ("p",), 3, 0, True)
    assert api._check(request(inputs, "q" * api.MAX_QUERY_CHARS)).text == "q" * api.MAX_QUERY_CHARS


@pytest.mark.parametrize(
    "change,detail",
    [
        ({"schema_version": "3.0"}, "unsupported schema_version"),
        ({"schema_version": 2}, "unsupported schema_version"),
        ({"index_generation": "g"}, "request carries unknown fields"),
        ({"access_domain": ""}, "access_domain must be a non-empty string"),
        ({"query": "  "}, "query must be a non-empty string"),
        ({"query": 5}, "query must be a non-empty string"),
        ({"query": "q" * 2001}, "query must be at most 2000 characters"),
        ({"projects": "p"}, "projects must be a list of non-empty strings"),
        ({"projects": ["p", ""]}, "projects must be a list of non-empty strings"),
        ({"projects": [3]}, "projects must be a list of non-empty strings"),
        ({"limit": True}, "limit and context must be integers"),
        ({"limit": "3"}, "limit and context must be integers"),
        ({"context": 1.0}, "limit and context must be integers"),
        ({"group_sessions": 1}, "group_sessions must be a boolean"),
    ],
)
def test_check_refuses_a_bad_envelope(inputs, change, detail):
    with pytest.raises(Rejected) as raised:
        api._check({**request(inputs, "find"), **change})
    assert (raised.value.error_class, raised.value.detail) == ("invalid_request", detail)


def test_check_refuses_a_request_that_is_not_an_object():
    with pytest.raises(Rejected) as raised:
        api._check(["find"])
    assert raised.value.detail == "request must be an object"


def test_search_answers_with_operation_id(inputs, corpus):
    body = request(inputs, inputs["queries"]["symbol"])
    answer = api.search(corpus.searcher, grant(inputs), body, embedder(inputs))
    assert answer["schema_version"] == SCHEMA_VERSION and answer["state"] == "answered"
    assert answer["operation_id"] == f"search:{digest(body)[:16]}"
    assert answer["results"] == corpus.searcher.search(grant(inputs), api._check(body), embedder(inputs))["results"]


def test_search_refusals_map_to_classes(inputs, corpus, monkeypatch):
    scoped = api.search(corpus.searcher, grant(inputs), request(inputs, "x", projects=[inputs["unauthorized_project"]]))
    assert scoped == {
        "schema_version": SCHEMA_VERSION,
        "operation_id": scoped["operation_id"],
        "state": "rejected",
        "error": {"class": "forbidden_scope", "retry": "never", "detail": search.SCOPE_REFUSED},
    }
    bounded = api.search(corpus.searcher, grant(inputs), request(inputs, "x", limit=0))
    assert bounded["error"] == {"class": "invalid_request", "retry": "never", "detail": search.BAD_BOUNDS}
    invalid = api.search(corpus.searcher, grant(inputs), {"query": "x"})
    assert invalid["error"]["detail"] == "unsupported schema_version"

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(corpus.searcher, "search", locked)
    failed = api.search(corpus.searcher, grant(inputs), request(inputs, "x"))
    assert failed["error"] == {"class": "dependency_unavailable", "retry": "same_request", "detail": api.SEARCH_FAILED}


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_idx04_cases.run_case(case), sv2_idx04_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)


def test_the_package_result_names_the_case_records_and_states():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, value in sv2_idx04_cases.run_case("a")["input_sha256"].items():
        assert manifest["inputs"][f"tests/fixtures/swarm_v2/{name}"] == value
    assert manifest["cases"] == ["T-SV2-IDX-04-A", "T-SV2-IDX-04-B", "T-SV2-IDX-04-C"]
    assert result["package"] == "SV2-IDX-04"
    assert result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    assert set(result["metrics"]) == {"session_search_relevance", search.SCOPE_REJECTIONS}
    for case in "abc":
        assert (EVIDENCE.parents[1] / result["cases"][case]).exists()
