"""Tests for SV2-PRF-04 brain context usefulness on held out tasks."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.evaluation.brain_context import cases, evaluate
from tests.evaluation.brain_context.evaluate import POLICIES, Refused, Registry

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[1]
EVIDENCE = ROOT / "evidence" / "SV2-PRF-04"


@pytest.fixture
def corpus():
    return evaluate.load_corpus()


def totals(**changes):
    base = {
        "context_source_recall": 0.6,
        "reviewed_usefulness": 0.8,
        "repeated_mistakes_avoided": 0.5,
        "scope_leakage": 0,
        "irrelevant_injection": 1,
        "stale_injection": 0,
        "arcs": 5,
        "context_tokens": 500,
    }
    return {**base, **changes}


def test_terms_drop_function_words_and_short_tokens():
    assert evaluate.terms("Why does the merge queue refuse a push") == {"merge", "queue", "refuse", "push"}


def test_scope_applies_before_graph_expansion_across_an_accepted_edge(corpus):
    fixture = corpus["fixtures"]["alpha"]
    task = fixture["tasks"][0]
    wide = evaluate.Policy("probe", hops=3, statuses=("accepted", "proposed"), limit=20, tokens=10_000)
    selected = evaluate.pack(wide, task, fixture, corpus["budget"])["selected"]
    assert "alpha-incident-lesson" in selected
    assert not {"alpha-foreign-lesson", "alpha-foreign-arc"} & set(selected)


def test_heat_admits_only_in_the_feed(corpus):
    fixture = corpus["fixtures"]["alpha"]
    task = next(row for row in fixture["tasks"] if row["case"] == "unrelated_hot_arc")
    feed = evaluate.pack(POLICIES["feed_only"], task, fixture, corpus["budget"])["selected"]
    scoped = evaluate.pack(POLICIES["scoped"], task, fixture, corpus["budget"])["selected"]
    assert feed[0] == "alpha-hot-arc" and "alpha-hot-arc" not in scoped


def test_superseded_guidance_yields_only_to_a_visible_successor():
    old = {"id": "old", "superseded_by": "new"}
    kept, omitted = evaluate.current({"old": old, "new": {"id": "new"}})
    assert set(kept) == {"new"} and omitted == ["old"]
    kept, omitted = evaluate.current({"old": old})
    assert set(kept) == {"old"} and omitted == []


def test_the_token_ceiling_skips_an_item_that_does_not_fit_and_keeps_filling(corpus):
    fixture = corpus["fixtures"]["alpha"]
    task = fixture["tasks"][0]
    packed = evaluate.pack(POLICIES["graph"], task, fixture, {"tokens": 100, "limit": 6})
    assert packed["selected"] == ["alpha-incident-lesson"] and packed["omitted_over_budget"] == 1


def test_score_counts_leakage_without_naming_out_of_scope_items(corpus):
    fixture = corpus["fixtures"]["alpha"]
    task = fixture["tasks"][0]
    scored = evaluate.score(
        task,
        {
            "selected": ["alpha-foreign-arc", "alpha-incident-arc"],
            "tokens": 280,
            "omitted_superseded": 0,
            "omitted_over_budget": 0,
        },
        fixture,
    )
    assert scored["injected"] == ["alpha-incident-arc"] and scored["scope_leakage"] == 1
    assert scored["found"] == 1 and scored["repeated_mistake_avoided"] is False


def test_more_arcs_or_tokens_alone_never_qualify():
    verdict = evaluate.qualify(totals(), totals(arcs=9, context_tokens=1200))
    assert verdict == {
        "qualified": False,
        "reason": "no_gain",
        "gains": [],
        "regressions": [],
        "volume_delta": {"arcs": 4, "context_tokens": 700},
    }


def test_a_gain_with_any_regression_or_leakage_is_refused():
    assert evaluate.qualify(totals(), totals(context_source_recall=1.0))["qualified"] is True
    noisy = evaluate.qualify(totals(), totals(context_source_recall=1.0, irrelevant_injection=2))
    assert noisy["reason"] == "regressed" and noisy["regressions"] == ["irrelevant_injection"]
    leaking = evaluate.qualify(totals(scope_leakage=1), totals(context_source_recall=1.0, scope_leakage=1))
    assert leaking["regressions"] == ["scope_leakage"]


def test_the_registry_refuses_a_stale_sequence_and_replays_an_operation_once(tmp_path):
    registry = Registry(tmp_path / "accepted.json")
    registry.seed("rev-1", "scoped", "a" * 64)
    promote = {
        "operation_id": "op-1",
        "kind": "promote",
        "corpus_revision": "rev-1",
        "expected_sequence": 1,
        "policy": "graph",
        "report_sha256": "b" * 64,
        "verdict": {"qualified": True},
    }
    assert registry.submit(promote) == {"state": "accepted", "sequence": 2}
    assert registry.submit(promote) == {"state": "replayed", "sequence": 2}
    before = registry.path.read_bytes()
    with pytest.raises(Refused, match="stale_sequence"):
        registry.submit({**promote, "operation_id": "op-2"})
    with pytest.raises(Refused, match="never_accepted"):
        registry.submit(
            {**promote, "operation_id": "op-3", "kind": "revert", "expected_sequence": 2, "policy": "feed_only"}
        )
    assert registry.path.read_bytes() == before and len(registry.read()["history"]) == 2


def test_the_registry_refuses_a_reseed_a_missing_verdict_and_a_reused_id_of_another_kind(tmp_path):
    registry = Registry(tmp_path / "accepted.json")
    registry.seed("rev-1", "scoped", "a" * 64)
    with pytest.raises(Refused, match="already_seeded"):
        registry.seed("rev-1", "graph", "b" * 64)
    promote = {
        "operation_id": "op-1",
        "kind": "promote",
        "corpus_revision": "rev-1",
        "expected_sequence": 1,
        "policy": "graph",
        "report_sha256": "b" * 64,
        "verdict": None,
    }
    with pytest.raises(Refused, match="not_qualified"):
        registry.submit(promote)
    registry.submit({**promote, "verdict": {"qualified": True}})
    with pytest.raises(Refused, match="operation_id_reused"):
        registry.submit({**promote, "kind": "revert", "expected_sequence": 2})
    assert registry.read()["sequence"] == 2 and registry.read()["history"][0]["policy"] == "scoped"


def test_the_measure_command_prints_totals_per_policy(capsys):
    assert evaluate.main(["--fixture", "beta", "--policy", "graph"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert list(printed) == ["graph"] and printed["graph"]["context_source_recall"] == 1.0


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first = cases.run_case(case)
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)


def test_the_manifest_hashes_the_corpus_and_the_harness():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, digest in manifest["inputs"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    assert manifest["cases"] == ["T-SV2-PRF-04-A", "T-SV2-PRF-04-B", "T-SV2-PRF-04-C"]
    assert result["package"] == "SV2-PRF-04" and result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    assert set(result["metrics"]) >= {"context_source_recall", "reviewed_usefulness"}
    for case in "abc":
        assert (ROOT / result["cases"][case]).exists()
