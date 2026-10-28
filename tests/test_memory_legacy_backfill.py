"""SV2-MIG-03: idempotent legacy backfill into the session corpus."""

import hashlib
import json
from pathlib import Path

import pytest

from hooks.memory import legacy_backfill as backfill
from hooks.memory.api.ingest import Grant, Rejected
from hooks.memory.catalog import Catalog
from tests import sv2_mig03_cases as cases

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-MIG-03"
GRANT = Grant("operator", "corpus", frozenset({"proj"}))
ALIASES = {"/work/proj": "proj"}


def raw(session: str, cwd: str, *texts: str) -> bytes:
    lines = [
        {
            "type": "user",
            "uuid": f"u{index}",
            "sessionId": session,
            "cwd": cwd,
            "timestamp": f"2026-01-0{index + 1}T00:00:00Z",
            "message": {"role": "user", "content": text},
        }
        for index, text in enumerate(texts)
    ]
    return "".join(json.dumps(line) + "\n" for line in lines).encode()


def source(native: str = "s1", installation: str = "laptop", location: str = "a/s1.jsonl", **material) -> dict:
    return {
        "provider": "claude",
        "installation": installation,
        "native_session_id": native,
        "location": location,
        **material,
    }


def make_plan(*sources: dict, batch_size: int = 2) -> backfill.BackfillPlan:
    return backfill.BackfillPlan("corpus", tuple(sources), ALIASES, batch_size)


def stores(tmp_path: Path) -> tuple[Catalog, backfill.BackfillLedger]:
    return Catalog(tmp_path / "catalog.db"), backfill.BackfillLedger(tmp_path / "nested" / "backfill.db")


def run(tmp_path: Path, plan: backfill.BackfillPlan, run_id: str = "r1", limit: int | None = None) -> dict:
    catalog, ledger = stores(tmp_path)
    try:
        return backfill.backfill(catalog, ledger, GRANT, plan, run_id, limit)
    finally:
        catalog.close()
        ledger.close()


@pytest.mark.parametrize(
    ("run_id", "plan", "error"),
    [
        ("", make_plan(), ("invalid_plan", "a run id must be a non-empty string")),
        (7, make_plan(), ("invalid_plan", "a run id must be a non-empty string")),
        ("r", make_plan(batch_size=0), ("invalid_plan", "batch_size must be a positive integer")),
        ("r", make_plan(batch_size=True), ("invalid_plan", "batch_size must be a positive integer")),
        ("r", make_plan(batch_size=1.0), ("invalid_plan", "batch_size must be a positive integer")),
        (
            "r",
            make_plan(source(), "bad"),
            ("invalid_plan", "source 1 needs a provider, installation, native_session_id and location"),
        ),
        (
            "r",
            make_plan({**source(), "location": ""}),
            ("invalid_plan", "source 0 needs a provider, installation, native_session_id and location"),
        ),
        (
            "r",
            make_plan({**source(), "installation": 3}),
            ("invalid_plan", "source 0 needs a provider, installation, native_session_id and location"),
        ),
        (
            "r",
            make_plan({**source(), "provider": "gemini"}),
            ("invalid_plan", "source 0 provider is not claude or codex"),
        ),
        (
            "r",
            backfill.BackfillPlan("corpus", (), {"work/proj": "proj"}),
            ("invalid_plan", "alias folders must be absolute paths"),
        ),
        (
            "r",
            backfill.BackfillPlan("corpus", (), {"/work/../proj": "proj"}),
            ("invalid_plan", "alias folders must be absolute paths"),
        ),
        (
            "r",
            backfill.BackfillPlan("corpus", (), {"/work/x": "other"}),
            ("forbidden_scope", "an alias names a project outside the grant"),
        ),
        (
            "r",
            backfill.BackfillPlan("elsewhere", (), ALIASES),
            ("forbidden_scope", "the plan is not approved for the granted corpus; nothing was imported"),
        ),
    ],
)
def test_bad_plans_are_refused_before_any_write(tmp_path, run_id, plan, error):
    result = run(tmp_path, plan, run_id)
    assert result == {"state": "rejected", "error": {"class": error[0], "detail": error[1]}}
    ledger = backfill.BackfillLedger(tmp_path / "nested" / "backfill.db")
    assert ledger.checkpoint("r") == 0
    assert ledger.db.execute("SELECT COUNT(*) FROM legacy_backfill_run").fetchone()[0] == 0


def test_codex_sources_and_an_empty_plan_are_accepted(tmp_path):
    result = run(tmp_path, backfill.BackfillPlan("corpus", (), ALIASES, 1))
    assert result["state"] == "completed"
    assert result["batches"] == {"done": 0, "total": 0}
    assert backfill._source_error(0, {**source(), "provider": "codex"}) == ""


def test_duplicate_copies_import_once_with_every_reference_and_the_highest_fidelity(tmp_path):
    data = raw("s1", "/work/proj/sub", "one", "two")
    preview = [{"entry_type": "user", "timestamp": "", "content": "one", "tool_names": [], "uuid": "u0"}]
    plan = make_plan(
        source(location="b/s1.jsonl", raw=data),
        source(location="a/s1.jsonl", raw=data),
        source(location="legacy/s1", preview=preview),
    )
    result = run(tmp_path, plan)
    assert result["sessions"] == [{"session_uid": "corpus:claude:laptop:s1", "outcome": "committed"}]
    assert result["verified_alias_events"] == 2
    assert result["unknown_attribution_events"] == 0
    ledger = backfill.BackfillLedger(tmp_path / "nested" / "backfill.db")
    [held] = ledger.sessions("r1")
    sha = hashlib.sha256(data).hexdigest()
    assert held == {
        "session_uid": "corpus:claude:laptop:s1",
        "source_generation": f"import:{sha}",
        "run_id": "r1",
        "records": 2,
        "active": True,
        "provenance": {
            "provider": "claude",
            "installation": "laptop",
            "native_session_id": "s1",
            "source_references": ["a/s1.jsonl", "b/s1.jsonl"],
            "fidelity": "full_raw",
            "confidence": "lossless",
            "source_digest": sha,
            "importer_version": backfill.IMPORTER_VERSION,
            "migration_run": "r1",
            "first_event_time": "2026-01-01T00:00:00Z",
            "last_event_time": "2026-01-02T00:00:00Z",
            "verified_projects": ["proj"],
            "unknown_attribution_events": 0,
        },
    }


def test_equal_native_ids_on_two_machines_stay_two_sessions(tmp_path):
    plan = make_plan(
        source(installation="laptop", raw=raw("s1", "/work/proj", "laptop")),
        source(installation="desktop", raw=raw("s1", "/srv/proj", "desktop")),
    )
    result = run(tmp_path, plan)
    assert [item["session_uid"] for item in result["sessions"]] == [
        "corpus:claude:desktop:s1",
        "corpus:claude:laptop:s1",
    ]
    assert result["verified_alias_events"] == 1
    assert result["unknown_attribution_events"] == 1


def test_conflicting_copies_and_forged_digests_are_quarantined(tmp_path):
    first, second = raw("s1", "/work/proj", "one"), raw("s1", "/work/proj", "two")
    good = raw("s2", "/work/proj", "kept")
    plan = make_plan(
        source(location="a", raw=first),
        source(location="b", raw=second),
        source("s2", location="c", raw=good),
        source("s2", location="d", raw=good, raw_sha256="0" * 64),
        source("s3", location="e", raw=good, raw_sha256="0" * 64),
        source("s4", location="f"),
    )
    result = run(tmp_path, plan)
    digests = sorted(hashlib.sha256(data).hexdigest() for data in (first, second))
    assert result["quarantined"] == [
        {"session_uid": "corpus:claude:laptop:s1", "reason": backfill.CONFLICT, "detail": digests[0]},
        {"session_uid": "corpus:claude:laptop:s1", "reason": backfill.CONFLICT, "detail": digests[1]},
        {"session_uid": "corpus:claude:laptop:s2", "reason": backfill.REFUSED, "detail": "d: raw_hash_mismatch"},
        {"session_uid": "corpus:claude:laptop:s3", "reason": backfill.REFUSED, "detail": "e: raw_hash_mismatch"},
    ]
    assert [item["outcome"] for item in result["sessions"]] == ["quarantined", "committed", "quarantined", "missing"]
    assert result["missing"] == ["corpus:claude:laptop:s4"]
    catalog = Catalog(tmp_path / "catalog.db")
    assert catalog.events("corpus", "corpus:claude:laptop:s1", f"import:{digests[0]}") == []


def test_preview_and_vector_sources_import_with_their_timestamps_and_unknown_projects():
    preview = [
        {"entry_type": "assistant", "timestamp": "t2", "content": "hi", "tool_names": ["Edit"], "uuid": "p1"},
        {"entry_type": "summary", "timestamp": "", "content": "sum", "tool_names": [], "uuid": "p2"},
        {"entry_type": "odd", "timestamp": "t3", "content": "?", "tool_names": [], "uuid": "p3"},
    ]
    vectors = [
        {"chunk_idx": 4, "timestamp": "t9", "text_preview": "late"},
        {"chunk_idx": 1, "timestamp": "t1", "text_preview": "early"},
    ]
    [preview_item, vector_item] = backfill._work(
        make_plan(source("p", location="lp", preview=preview), source("v", location="lv", vectors=vectors))
    )
    assert preview_item["generation"].startswith("legacy:")
    assert preview_item["events"] == [
        {
            "sequence": 1,
            "byte_start": 0,
            "byte_end": 0,
            "project_id": None,
            "record": {
                "kind": "assistant_text",
                "text": "hi",
                "tool_names": ["Edit"],
                "legacy_uuid": "p1",
                "source_time": "t2",
            },
        },
        {
            "sequence": 2,
            "byte_start": 0,
            "byte_end": 0,
            "project_id": None,
            "record": {
                "kind": "system_text",
                "text": "sum",
                "tool_names": [],
                "legacy_uuid": "p2",
                "source_time": None,
            },
        },
        {
            "sequence": 3,
            "byte_start": 0,
            "byte_end": 0,
            "project_id": None,
            "record": {"kind": None, "text": "?", "tool_names": [], "legacy_uuid": "p3", "source_time": "t3"},
        },
    ]
    assert [event["record"] for event in vector_item["events"]] == [
        {"kind": "meta", "text": "early", "legacy_chunk_idx": 1, "source_time": "t1"},
        {"kind": "meta", "text": "late", "legacy_chunk_idx": 4, "source_time": "t9"},
    ]
    assert [event["sequence"] for event in vector_item["events"]] == [1, 2]
    found = backfill.provenance(preview_item, "r")
    assert (found["fidelity"], found["confidence"]) == ("preview_only", "lossy")
    assert (found["first_event_time"], found["last_event_time"]) == ("t2", "t3")
    assert found["unknown_attribution_events"] == 3
    assert found["verified_projects"] == []


def test_a_session_without_times_reports_none_and_a_raw_without_records_imports_nothing(tmp_path):
    vectors = [{"chunk_idx": 0, "timestamp": "", "text_preview": "x"}]
    [item] = backfill._work(make_plan(source(vectors=vectors)))
    assert backfill.provenance(item, "r")["first_event_time"] is None
    assert backfill.provenance(item, "r")["last_event_time"] is None
    result = run(tmp_path, make_plan(source(raw=b"\n")))
    assert result["sessions"] == [{"session_uid": "corpus:claude:laptop:s1", "outcome": "no_records"}]
    assert result[backfill.METRIC] == 0
    assert result["remaining_sessions"] == []


def test_a_limit_stops_mid_batch_and_the_rerun_resumes_at_the_checkpoint(tmp_path):
    plan = make_plan(*(source(f"s{index}", raw=raw(f"s{index}", "/work/proj", "a", "b")) for index in range(5)))
    stopped = run(tmp_path, plan, limit=3)
    assert stopped["state"] == "partial"
    assert stopped["batches"] == {"done": 1, "total": 3}
    assert [item["session_uid"][-2:] for item in stopped["sessions"]] == ["s0", "s1", "s2"]
    assert stopped[backfill.METRIC] == 4
    assert stopped["remaining_sessions"] == ["corpus:claude:laptop:s3", "corpus:claude:laptop:s4"]
    nothing = run(tmp_path, plan, limit=0)
    assert (nothing["state"], nothing["sessions"]) == ("partial", [])
    resumed = run(tmp_path, plan)
    assert [item["outcome"] for item in resumed["sessions"]] == ["already_imported", "committed", "committed"]
    assert (resumed["state"], resumed["batches"], resumed[backfill.METRIC]) == ("completed", {"done": 3, "total": 3}, 0)
    again = run(tmp_path, plan)
    assert (again["state"], again["sessions"]) == ("completed", [])
    catalog = Catalog(tmp_path / "catalog.db")
    assert catalog.db.execute("SELECT COUNT(*) FROM session_event").fetchone()[0] == 10
    assert catalog.duplicate_events_total("corpus") == 0


def test_another_run_id_imports_nothing_again(tmp_path):
    plan = make_plan(source(raw=raw("s1", "/work/proj", "a")))
    run(tmp_path, plan)
    second = run(tmp_path, plan, "r2")
    assert second["sessions"] == [{"session_uid": "corpus:claude:laptop:s1", "outcome": "already_imported"}]
    ledger = backfill.BackfillLedger(tmp_path / "nested" / "backfill.db")
    assert ledger.sessions("r2") == []
    assert [row["run_id"] for row in ledger.sessions("r1")] == ["r1"]


def test_a_session_committed_before_its_mark_replays_without_a_second_copy(tmp_path):
    plan = make_plan(source(raw=raw("s1", "/work/proj", "a")))
    [item] = backfill._work(plan)
    catalog, ledger = stores(tmp_path)
    request = backfill._request(backfill._Run(catalog, ledger, GRANT, "r1"), item)
    assert request["batch_id"] == request["source_generation"] == item["generation"]
    assert request["schema_version"] == backfill.SCHEMA_VERSION
    backfill.ingest(catalog, GRANT, request)
    result = backfill.backfill(catalog, ledger, GRANT, plan, "r1")
    assert result["sessions"] == [{"session_uid": "corpus:claude:laptop:s1", "outcome": "replayed"}]
    assert len(catalog.events("corpus", item["session_uid"], item["generation"])) == 1
    assert ledger.imported(item["session_uid"], item["generation"])


def test_an_event_identity_conflict_is_quarantined_and_other_refusals_keep_their_class(tmp_path, monkeypatch):
    plan = make_plan(source(raw=raw("s1", "/work/proj", "a")), source("s2", raw=raw("s2", "/work/proj", "b")))
    item = backfill._work(plan)[0]
    catalog, ledger = stores(tmp_path)
    earlier = {**item["events"][0], "record": {"kind": "user_text", "text": "other"}}
    backfill.ingest(
        catalog,
        GRANT,
        {**backfill._request(backfill._Run(catalog, ledger, GRANT, "r"), item), "batch_id": "x", "events": [earlier]},
    )
    result = backfill.backfill(catalog, ledger, GRANT, plan, "r1")
    assert result["quarantined"] == [
        {"session_uid": item["session_uid"], "reason": backfill.IDENTITY_CONFLICT, "detail": item["generation"]}
    ]
    assert [event["record"]["text"] for event in catalog.events("corpus", item["session_uid"], item["generation"])] == [
        "other"
    ]
    assert result["remaining_sessions"] == [item["session_uid"]]
    monkeypatch.setattr(backfill, "ingest", lambda *_: {"state": "rejected", "error": {"class": "invalid_request"}})
    other = backfill.backfill(catalog, ledger, GRANT, make_plan(source("s9", raw=raw("s9", "/w", "c"))), "r2")
    assert other["quarantined"][0]["reason"] == "invalid_request"


def test_a_catalog_outage_interrupts_without_quarantine_and_resumes(tmp_path, monkeypatch):
    plan = make_plan(source("s1", raw=raw("s1", "/work/proj", "a")), source("s2", raw=raw("s2", "/work/proj", "b")))
    real = backfill.ingest
    monkeypatch.setattr(backfill, "ingest", lambda *_: {"state": "rejected", "error": {"class": backfill.UNAVAILABLE}})
    stopped = run(tmp_path, plan)
    assert (stopped["state"], stopped["sessions"], stopped["quarantined"]) == ("interrupted", [], [])
    assert stopped[backfill.METRIC] == 2
    monkeypatch.setattr(backfill, "ingest", real)
    assert run(tmp_path, plan)["state"] == "completed"


def test_the_checkpoint_moves_only_from_the_expected_batch(tmp_path):
    ledger = backfill.BackfillLedger(tmp_path / "ledger.db")
    assert ledger.checkpoint("r") == 0
    assert ledger.open("r", "d1") == 0
    ledger.advance("r", 0, 1)
    assert ledger.open("r", "d1") == 1
    with pytest.raises(Rejected) as stale:
        ledger.advance("r", 0, 1)
    assert (stale.value.error_class, stale.value.detail) == ("stale_checkpoint", backfill.STALE)
    with pytest.raises(Rejected) as moved:
        ledger.open("r", "d2")
    assert (moved.value.error_class, moved.value.detail) == ("plan_mismatch", backfill.PLAN_MISMATCH)
    assert ledger.checkpoint("r") == 1


def test_deactivate_hides_only_the_run_projection(tmp_path):
    plan = make_plan(source(raw=raw("s1", "/work/proj", "a")))
    run(tmp_path, plan)
    run(tmp_path, make_plan(source("s2", raw=raw("s2", "/work/proj", "b"))), "r2")
    ledger = backfill.BackfillLedger(tmp_path / "nested" / "backfill.db")
    assert ledger.deactivate("r1") == 1
    assert ledger.deactivate("r1") == 0
    assert [row["active"] for row in ledger.sessions("r1")] == [False]
    assert [row["active"] for row in ledger.sessions("r2")] == [True]
    catalog = Catalog(tmp_path / "catalog.db")
    assert catalog.db.execute("SELECT COUNT(*) FROM session_event").fetchone()[0] == 2
    [shown] = ledger.projection(catalog, "corpus")
    assert shown["session_uid"] == "corpus:claude:laptop:s2"
    assert [event["record"]["text"] for event in shown["events"]] == ["b"]
    assert ledger.projection(catalog, "corp") == []


def test_the_ledger_creates_missing_parent_folders_and_projects_its_generation(tmp_path):
    ledger = backfill.BackfillLedger(tmp_path / "a" / "b" / "c.db")
    ledger.mark("r", "corpus:claude:laptop:s1", "g1", 0, {"x": 1})
    catalog = Catalog(tmp_path / "catalog.db")
    assert ledger.projection(catalog, "corpus") == [
        {"session_uid": "corpus:claude:laptop:s1", "source_generation": "g1", "events": []}
    ]


@pytest.mark.parametrize("changed", ["content", "batch_size"])
def test_a_run_id_is_bound_to_its_generations_and_batch_size(tmp_path, changed):
    first = make_plan(source(raw=raw("s1", "/work/proj", "a")))
    run(tmp_path, first, limit=0)
    other = (
        make_plan(source(raw=raw("s1", "/work/proj", "b")))
        if changed == "content"
        else make_plan(source(raw=raw("s1", "/work/proj", "a")), batch_size=3)
    )
    refused = run(tmp_path, other)
    assert refused == {"state": "rejected", "error": {"class": "plan_mismatch", "detail": backfill.PLAN_MISMATCH}}


def test_a_report_names_its_run_and_importer_version(tmp_path):
    result = run(tmp_path, make_plan(source(raw=raw("s1", "/work/proj", "a"))), "r7")
    assert (result["run_id"], result["importer_version"]) == ("r7", backfill.IMPORTER_VERSION)


def test_the_command_line_requires_a_command_and_a_corpus(tmp_path, capsys):
    with pytest.raises(SystemExit):
        backfill.main([])
    with pytest.raises(SystemExit):
        backfill.main(["run", str(tmp_path / "c.db"), str(tmp_path / "l.db"), str(tmp_path / "p.json"), "r1"])


def test_the_command_line_grants_no_project_by_default_and_passes_its_limit(tmp_path, capsys):
    (tmp_path / "s1.jsonl").write_bytes(raw("s1", "/elsewhere", "a"))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps({"corpus": "corpus", "sources": [source(location="s1.jsonl", raw="s1.jsonl")]}))
    assert backfill.load_plan(plan_file).aliases == {}
    base = ["run", tmp_path / "c.db", tmp_path / "l.db", plan_file]
    code, stopped = cli(capsys, *base, "r1", "--corpus", "corpus", "--limit", "0")
    assert (code, stopped["state"], stopped[backfill.METRIC]) == (0, "partial", 1)
    code, done = cli(capsys, *base, "r1", "--corpus", "corpus")
    assert (code, done["state"], done["unknown_attribution_events"]) == (0, "completed", 1)
    bad = tmp_path / "bad.json"
    forged = source("s2", location="s1.jsonl", raw="s1.jsonl", raw_sha256="0" * 64)
    bad.write_text(json.dumps({"corpus": "corpus", "sources": [forged]}))
    cli(capsys, "run", tmp_path / "c.db", tmp_path / "l.db", bad, "r2", "--corpus", "corpus")
    code, report = cli(capsys, "report", tmp_path / "l.db", "r2")
    assert (code, report["run_id"]) == (0, "r2")
    assert report["quarantined"] == [
        {"session_uid": "corpus:claude:laptop:s2", "reason": backfill.REFUSED, "detail": "s1.jsonl: raw_hash_mismatch"}
    ]


def test_gaps_keep_a_finished_run_from_reading_as_a_clean_completion(tmp_path):
    assert run(tmp_path, make_plan(source(location="nothing")))["state"] == backfill.GAPS
    forged = make_plan(source("s2", raw=raw("s2", "/work/proj", "b"), raw_sha256="0" * 64))
    assert run(tmp_path, forged, "r2")["state"] == backfill.GAPS
    assert run(tmp_path, make_plan(source("s3", raw=raw("s3", "/work/proj", "c"))), "r3")["state"] == "completed"


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
    expected = {
        path.relative_to(cases.CASES_MODULE.parent).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in inputs
    }
    assert found["input_sha256"] == expected
    assert found["fixture_inputs"] == record["fixture_inputs"] == cases.FIXTURE_INPUTS
    assert found["tested_commit"] == record["tested_commit"]
    assert record["evidence_class"] == cases.EVIDENCE_CLASS


def cli(capsys, *argv) -> tuple[int, dict]:
    code = backfill.main([str(arg) for arg in argv])
    return code, json.loads(capsys.readouterr().out)


def test_the_command_line_runs_reports_and_deactivates(tmp_path, capsys):
    folder = tmp_path / "plan"
    folder.mkdir()
    (folder / "s1.jsonl").write_bytes(raw("s1", "/work/proj", "a"))
    doc = {"corpus": "corpus", "aliases": ALIASES, "sources": [source(location="s1.jsonl", raw="s1.jsonl"), "bad"]}
    (folder / "plan.json").write_text(json.dumps(doc))
    catalog, ledger, plan_file = tmp_path / "c.db", tmp_path / "l.db", folder / "plan.json"
    code, refused = cli(capsys, "run", catalog, ledger, plan_file, "r1", "--corpus", "corpus", "--project", "proj")
    assert (code, refused["error"]["class"]) == (1, "invalid_plan")
    doc["sources"] = doc["sources"][:1]
    (folder / "plan.json").write_text(json.dumps(doc))
    code, done = cli(
        capsys, "run", catalog, ledger, plan_file, "r1", "--corpus", "corpus", "--project", "proj", "--limit", "5"
    )
    assert (code, done["state"], done["principal"], done["batches"]) == (
        0,
        "completed",
        "operator",
        {"done": 1, "total": 1},
    )
    code, report = cli(capsys, "report", ledger, "r1")
    assert (code, report["checkpoint"], report["quarantined"]) == (0, 1, [])
    assert report["sessions"][0]["provenance"]["source_references"] == ["s1.jsonl"]
    code, shown = cli(capsys, "projection", catalog, ledger, "corpus")
    assert (code, shown["corpus"], [item["events"] for item in shown["sessions"]]) == (0, "corpus", [1])
    assert cli(capsys, "deactivate", ledger, "r1") == (0, {"run_id": "r1", "deactivated": 1})
    assert cli(capsys, "projection", catalog, ledger, "corpus") == (0, {"corpus": "corpus", "sessions": []})
    loaded = backfill.load_plan(plan_file)
    assert loaded.batch_size == 50
    assert loaded.sources[0]["raw"] == (folder / "s1.jsonl").read_bytes()
