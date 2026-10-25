"""Tests for SV2-PRF-03 capture and retrieval under swarm load."""

import hashlib
import json
import random
from pathlib import Path

import pytest

from tests import sv2_prf03_cases as cases
from tests.performance.session_memory import load

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[1]
EVIDENCE = ROOT / "evidence" / "SV2-PRF-03"
SEARCH_P95_BUDGET_SECONDS = 0.5


def test_percentiles_rank_the_sorted_values_and_report_an_empty_sample():
    assert load.percentiles([3.0, 1.0, 2.0, 4.0]) == {"count": 4, "p50": 3.0, "p95": 4.0, "max": 4.0}
    assert load.percentiles([]) == {"count": 0, "p50": None, "p95": None, "max": None}


def test_a_marker_is_found_only_by_a_lexical_match_never_by_vector_similarity_alone():
    vector_only = {"results": [{"explain": {"lexical": None, "vector": {"rank": 1, "score": 0.4}}}]}
    lexical = {"results": [{"explain": {"lexical": {"rank": 1, "score": 1.2, "matched": ["m1x"]}, "vector": None}}]}
    assert load.found(vector_only) is False
    assert load.found(lexical) is True
    assert load.found({"state": "rejected"}) is False


def test_a_split_turn_leaves_half_of_its_last_line_until_the_writer_finishes(tmp_path):
    path = tmp_path / "transcript.jsonl"
    writer = load.Writer("writer-0", "session:abcdef0123456789", "project", path, random.Random(1))
    writer.append(1, 1.0, True)
    first = path.read_bytes()
    assert first.count(b"\n") == 1 and not first.endswith(b"\n") and writer.carry
    assert list(writer.completed.values()) == [1.0]
    writer.finish(2.0)
    lines = path.read_bytes().splitlines()
    assert [json.loads(item)["type"] for item in lines] == ["user", "assistant"]
    assert sorted(writer.completed.values()) == [1.0, 2.0] and writer.carry == b""
    assert writer.markers == [(1, "m23456789t1x")]


def test_a_dropping_uploader_is_graded_by_the_missing_events_and_archive_gap():
    failures = ["writer-1: 4 events for 8 complete lines", "writer-1: archive the archive does not cover the range"]
    assert cases.failure_kinds(failures) == {"archive_incomplete": 1, "events_missing": 1}


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first = cases.run_case(case)
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)


def test_the_manifest_hashes_the_workload_and_the_harness():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, digest in manifest["inputs"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    assert manifest["cases"] == ["T-SV2-PRF-03-A", "T-SV2-PRF-03-B", "T-SV2-PRF-03-C"]
    assert result["package"] == "SV2-PRF-03" and result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    assert set(result["metrics"]) == {"ingest_ack_latency_seconds", "search_latency_seconds"}
    for case in "abc":
        assert (ROOT / result["cases"][case]).exists()


@pytest.mark.wall_clock
def test_search_stays_within_its_wall_clock_budget_under_the_load():
    measured = load.measure(load.load_spec()["seeds"][0], load.load_spec())
    assert measured["verdict"]["complete"], measured["verdict"]["failures"]
    latency = measured["search_latency_seconds"]
    assert latency["count"] > 0 and latency["p95"] < SEARCH_P95_BUDGET_SECONDS, json.dumps(measured, indent=2)
