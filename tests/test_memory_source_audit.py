"""Tests for SV2-MIG-02 source fidelity audit of legacy transcript history."""

import hashlib
import json
from pathlib import Path

import pytest

from hooks.memory import source_audit as audit
from hooks.memory.api.ingest import Rejected
from tests import sv2_mig02_cases as cases

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-MIG-02"
SMALL = audit.LegacyLimits(preview_chars=5, preview_entries=2, vector_chars=12)


def raw(*lines: dict) -> bytes:
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def user(uuid: str, text: str) -> dict:
    return {"type": "user", "uuid": uuid, "message": {"content": text}}


def assistant(uuid: str, text: str, *tools: str) -> dict:
    blocks = [{"type": "text", "text": text}, *({"type": "tool_use", "name": name} for name in tools)]
    return {"type": "assistant", "uuid": uuid, "message": {"content": blocks}}


def preview(uuid: str, content: str) -> dict:
    return {"entry_type": "user", "timestamp": "", "content": content, "tool_names": [], "uuid": uuid}


def vector(index: int, text: str) -> dict:
    return {"chunk_idx": index, "timestamp": "", "text_preview": text}


def rejected(call) -> Rejected:
    with pytest.raises(Rejected) as error:
        call()
    return error.value


def test_raw_entries_read_text_tools_and_summaries_and_count_torn_lines():
    data = raw(user("u", "hi"), assistant("a", "ok", "Read", "Bash"), {"type": "summary", "uuid": "s", "summary": "x"})
    data += b'{"type": "progress"}\n\n{"type": "user", "uuid": "t", "mess'
    entries, unparsed = audit.raw_entries(data)
    assert entries == [
        {"uuid": "u", "entry_type": "user", "content": "hi", "tools": []},
        {"uuid": "a", "entry_type": "assistant", "content": "ok", "tools": ["Read", "Bash"]},
        {"uuid": "s", "entry_type": "summary", "content": "x", "tools": []},
    ]
    assert unparsed == 1


def test_turns_render_the_legacy_chunk_text():
    entries, _ = audit.raw_entries(raw(user("u1", "a"), assistant("a1", "b", "Read"), user("u2", "c")))
    assert audit.turns(entries) == ["User: a\nAssistant: b\nTools used: Read\n", "User: c\n"]
    assert audit.turns([]) == []


def test_full_raw_measures_capped_entries_and_missing_ranges_by_cause():
    lines = [user(f"u{n}", f"text number {n}") for n in range(6)]
    items = [preview("u1", "text "), preview("u2", "text "), preview("u4", "other")]
    found = audit.audit_source({"source": "s", "raw": raw(*lines), "preview": items}, SMALL)
    assert found["class"] == audit.FULL_RAW
    assert found["confidence"] == "lossless"
    assert found["raw"] == {"entries": 6, "turns": 6, "unparsed_lines": 0}
    assert found["preview"] == {
        "preview_entries": 3,
        "capped": 2,
        "diverged": 1,
        "missing_ranges": [
            {"start": 0, "end": 0, "cause": "trimmed_head"},
            {"start": 3, "end": 3, "cause": "incremental_offset_gap"},
            {"start": 5, "end": 5, "cause": "uncollected_tail"},
        ],
    }


def test_a_head_gap_below_the_entry_cap_is_not_called_trimmed():
    lines = [user("u0", "a"), user("u1", "b")]
    found = audit.audit_source({"source": "s", "raw": raw(*lines), "preview": [preview("u1", "b")]}, SMALL)
    assert found["preview"]["missing_ranges"] == [{"start": 0, "end": 0, "cause": "missing_head"}]


def test_vector_rows_are_matched_to_raw_turns_and_renumbering_is_reported():
    lines = [user("u0", "first"), user("u1", "second"), user("u2", "third")]
    rows = [vector(0, "User: first\n"), vector(1, "User: third\n"), vector(2, "User: gone\n")]
    found = audit.audit_source({"source": "s", "raw": raw(*lines), "vectors": rows}, SMALL)
    assert found["vectors"] == {
        "vector_rows": 3,
        "renumbered": [{"chunk_idx": 1, "turn": 2}],
        "orphans": 1,
        "turns_without_vector": 1,
    }
    assert found["preview"] is None


def test_vector_previews_compare_at_the_legacy_vector_cap():
    lines = [user("u0", "a long first turn")]
    found = audit.audit_source({"source": "s", "raw": raw(*lines), "vectors": [vector(0, "User: a long")]}, SMALL)
    assert found["vectors"]["renumbered"] == []
    assert found["vectors"]["orphans"] == 0


def test_preview_only_history_is_lossy_and_does_not_invent_missing_ranges():
    found = audit.audit_source({"source": "s", "preview": [preview("u", "12345"), preview("v", "1")]}, SMALL)
    assert found["class"] == audit.PREVIEW_ONLY
    assert found["confidence"] == "lossy"
    assert found["raw_sha256"] is None
    assert found["preview"] == {
        "preview_entries": 2,
        "possibly_capped": 1,
        "possibly_trimmed": True,
        "missing_ranges": "unknown",
    }


def test_vector_only_and_missing_sources_are_classified_apart():
    rows = [vector(0, "User: x\n")]
    assert audit.audit_source({"source": "s", "vectors": rows})["class"] == audit.VECTOR_ONLY
    assert audit.audit_source({"source": "s", "vectors": rows})["confidence"] == "fragment"
    missing = audit.audit_source({"source": "s", "preview": [], "vectors": []})
    assert (missing["class"], missing["confidence"]) == (audit.MISSING, "none")
    assert missing["preview_sha256"] is missing["vectors_sha256"] is None


def test_legacy_limits_default_to_the_retired_service_caps():
    assert audit.LegacyLimits() == audit.LegacyLimits(preview_chars=500, preview_entries=500, vector_chars=1000)


def test_source_hashes_are_recorded():
    data = raw(user("u", "x"))
    found = audit.audit_source({"source": "s", "raw": data, "raw_sha256": hashlib.sha256(data).hexdigest()})
    assert found["raw_sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {"source": "s", "class": "full_raw"},
        {"source": ""},
        {"source": 3},
        {"source": "s", "raw": "text"},
        {"source": "s", "preview": {}},
        {"source": "s", "preview": [{"uuid": "u", "content": "c"}]},
        {"source": "s", "preview": [{**preview("u", "c"), "content": 1}]},
        {"source": "s", "vectors": [vector(True, "x")]},
        {"source": "s", "vectors": [{**vector(0, "x"), "text_preview": None}]},
        {"source": "s", "vectors": ["row"]},
    ],
)
def test_malformed_observations_are_refused_without_echo(bad):
    error = rejected(lambda: audit.audit_source(bad))
    assert (error.error_class, error.detail) == ("bad_source", audit.BAD_SOURCE)


def test_a_raw_hash_mismatch_is_refused():
    error = rejected(lambda: audit.audit_source({"source": "s", "raw": b"x", "raw_sha256": "0"}))
    assert (error.error_class, error.detail) == ("raw_hash_mismatch", audit.HASH_MISMATCH)


def test_only_full_raw_sources_are_certified_lossless():
    held = audit.SourceAudit()
    held.run([{"source": "p", "preview": [preview("u", "c")]}, {"source": "r", "raw": b"{}"}], 1)
    assert held.certify("r") == {"source": "r", "label": "lossless", "raw_sha256": hashlib.sha256(b"{}").hexdigest()}
    assert rejected(lambda: held.certify("p")).detail == audit.NOT_LOSSLESS
    assert rejected(lambda: held.certify("q")).error_class == "unknown_source"


def test_rerun_upgrades_only_the_affected_source_and_never_downgrades():
    held = audit.SourceAudit()
    first = [{"source": "p", "preview": [preview("u", "c")]}, {"source": "m"}]
    assert held.run(first, 1) == {
        "p": {"outcome": "added", "observed": audit.PREVIEW_ONLY},
        "m": {"outcome": "added", "observed": audit.MISSING},
    }
    upgraded = held.run([{"source": "p", "raw": b"{}"}, {"source": "m"}], 2)
    assert upgraded == {
        "p": {"outcome": "upgraded", "observed": audit.FULL_RAW},
        "m": {"outcome": "replayed", "observed": audit.MISSING},
    }
    assert held.run(first, 3)["p"] == {"outcome": "kept", "observed": audit.PREVIEW_ONLY}
    assert held.state["revision"] == 2
    assert held.state["sources"]["p"]["class"] == audit.FULL_RAW


def test_a_stale_revision_is_refused_before_any_write():
    held = audit.SourceAudit()
    held.run([{"source": "p", "preview": [preview("u", "c")]}], 4)
    before = held.dumps()
    late = [{"source": "n"}, {"source": "p", "preview": [preview("u", "d")]}]
    assert rejected(lambda: held.run(late, 4)).error_class == "stale_revision"
    assert held.dumps() == before
    assert held.run(late, 5)["p"]["outcome"] == "updated"


def test_report_lists_each_class_separately_with_counts():
    held = audit.SourceAudit()
    held.run([{"source": "b", "raw": b""}, {"source": "a", "preview": [preview("u", "c")]}, {"source": "c"}], 1)
    report = held.report()
    assert [item["source"] for item in report["sources"]] == ["a", "b", "c"]
    assert (report["full_raw"], report["preview_only"], report["vector_only"], report["missing"]) == (
        ["b"],
        ["a"],
        [],
        ["c"],
    )
    assert report[audit.METRIC] == {"full_raw": 1, "preview_only": 1, "vector_only": 0, "missing": 1}
    assert list(report[audit.METRIC]) == ["full_raw", "preview_only", "vector_only", "missing"]


def test_state_round_trips_and_is_copied():
    state = {"revision": 1, "sources": {}}
    held = audit.SourceAudit(state, SMALL)
    held.run([{"source": "m"}], 2)
    assert state == {"revision": 1, "sources": {}}
    loaded = audit.SourceAudit.loads(held.dumps(), SMALL)
    assert (loaded.state, loaded.limits) == (held.state, SMALL)
    assert held.dumps() == json.dumps(held.state, sort_keys=True)


@pytest.mark.parametrize("case", ["A", "B", "C"])
def test_package_cases_match_committed_evidence(tmp_path, case):
    observed = json.loads(json.dumps(cases.run_case(case, tmp_path)))
    path = EVIDENCE / f"{case.lower()}-result.json"
    expected = json.loads(path.read_text()) if path.exists() else None
    assert observed == expected, json.dumps(observed, sort_keys=True)


def test_package_record_and_manifest_match_the_fixture_inputs():
    record = json.loads((EVIDENCE / "result.json").read_text())
    found = json.loads((EVIDENCE / "manifest.json").read_text())
    inputs = (cases.FIXTURE, *cases.RAW_FILES, cases.CASES_MODULE)
    assert found["input_sha256"] == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    assert found["fixture_inputs"] == record["fixture_inputs"] == cases.FIXTURE_INPUTS
    assert found["tested_commit"] == record["tested_commit"]
    assert record["evidence_class"] == cases.EVIDENCE_CLASS
