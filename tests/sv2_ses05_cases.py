import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import Catalog
from hooks.memory.importer import ImportLedger, ImportPlan, SourceRoot, import_sessions

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/session-import.json"
EVIDENCE_CLASS = (
    "mocked: fixture transcript files in a temporary folder and SQLite catalog and import ledger files stand in for "
    "the operator's real Claude and Codex homes and the central database; no live provider home was read"
)


class ResponseLost(BaseException):
    pass


class CrashAfterFirstCommit(Catalog):
    def commit(self, batch):
        result = super().commit(batch)
        if not getattr(self, "crashed", False):
            self.crashed = True
            raise ResponseLost
        return result


def materialize(inputs, root, files="files"):
    for relative, text in inputs[files].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def source_root(root, data):
    return SourceRoot(
        data["provider"], data["installation"], root / data["folder"], data["corpus"], tuple(data["expected"])
    )


def plan(inputs, root, *extra, aliases="aliases"):
    roots = [source_root(root, data) for data in inputs["roots"]] + [source_root(root, inputs[name]) for name in extra]
    return ImportPlan(tuple(roots), inputs[aliases])


def grant(inputs):
    data = inputs["grant"]
    return Grant(data["principal"], data["corpus"], frozenset(data["projects"]))


def stores(root):
    return Catalog(root / "catalog.sqlite3"), ImportLedger(root / "imports.sqlite3")


def outcomes(report):
    return {source["relative_path"]: source["outcome"] for source in report["sources"]}


def outcomes_hash(report):
    (source,) = [source for source in report["sources"] if source["outcome"] == "committed"]
    return source["source_sha256"]


def stored_events(catalog, imports, corpus):
    return {
        session["session_uid"]: catalog.events(corpus, session["session_uid"], session["source_generation"])
        for session in imports.sessions(corpus)
    }


def positive(inputs, root):
    materialize(inputs, root)
    catalog, imports = stores(root)
    corpus = inputs["grant"]["corpus"]
    dry = import_sessions(catalog, imports, grant(inputs), plan(inputs, root, "missing_root"), dry_run=True)
    assert catalog.snapshot() == {table: [] for table in catalog.snapshot()} and imports.sessions(corpus) == []
    report = import_sessions(catalog, imports, grant(inputs), plan(inputs, root, "missing_root"))
    assert set(outcomes(dry).values()) == {"would_import"}
    assert set(outcomes(report).values()) == {"committed"}
    events = stored_events(catalog, imports, corpus)
    projects = {uid: sorted({str(event["project_id"]) for event in found}) for uid, found in events.items()}
    assert projects["swarm-fixture:claude:workstation-a:5e6f-dup"] == ["None"]
    assert all("raw" not in event["record"] for found in events.values() for event in found)
    found = imports.search(catalog, grant(inputs), "ROLLOUT cursor")
    outsider = Grant("outsider", corpus, frozenset({"github.com/acme/other"}))
    assert {hit["project_id"] for hit in imports.search(catalog, outsider, "breaker")} == {None}
    assert found and all(hit["source"]["provider"] == "codex" for hit in found)
    assert imports.search(catalog, grant(inputs), "taxes") == []
    return {
        "dry_run": dry,
        "report": report,
        "provenance": imports.sessions(corpus),
        "event_projects": projects,
        "search_rollout_cursor": found,
        "search_breaker": imports.search(catalog, grant(inputs), "breaker"),
        "metrics": {"historical_import_unknown_attribution": imports.unknown_attribution(corpus)},
    }


def rejection(inputs, root):
    materialize(inputs, root)
    catalog, imports = stores(root)
    corpus = inputs["grant"]["corpus"]
    protected = (catalog.snapshot(), imports.snapshot())
    basename = import_sessions(catalog, imports, grant(inputs), plan(inputs, root, aliases="basename_aliases"))
    personal = import_sessions(catalog, imports, grant(inputs), plan(inputs, root, "personal_root"))
    assert (catalog.snapshot(), imports.snapshot()) == protected
    diagnostics = json.dumps([basename, personal], ensure_ascii=False)
    for hidden in ("taxes", "laptop-personal", "personal/projects", "9f-personal"):
        assert hidden not in diagnostics
    corrected = import_sessions(catalog, imports, grant(inputs), plan(inputs, root))
    events = stored_events(catalog, imports, corpus)
    assert "swarm-fixture:claude:laptop-personal:9f-personal" not in events
    duplicate = events["swarm-fixture:claude:workstation-a:5e6f-dup"]
    assert duplicate and {event["project_id"] for event in duplicate} == {None}
    return {
        "basename_alias": basename,
        "personal_root": personal,
        "protected_state_unchanged": True,
        "corrected_outcomes": outcomes(corrected),
        "duplicate_basename_projects": sorted({str(event["project_id"]) for event in duplicate}),
        "imported_sessions": sorted(events),
        "metrics": {"historical_import_unknown_attribution": imports.unknown_attribution(corpus)},
    }


def recovery(inputs, root):
    materialize(inputs, root)
    corpus = inputs["grant"]["corpus"]
    crashing, imports = CrashAfterFirstCommit(root / "catalog.sqlite3"), ImportLedger(root / "imports.sqlite3")
    with pytest.raises(ResponseLost):
        import_sessions(crashing, imports, grant(inputs), plan(inputs, root))
    survived = {"marked": imports.sessions(corpus), "catalog_events": len(crashing.snapshot()["session_event"])}
    crashing.close()
    imports.close()
    catalog, imports = stores(root)
    resumed = import_sessions(catalog, imports, grant(inputs), plan(inputs, root))
    before = catalog.snapshot()
    again = import_sessions(catalog, imports, grant(inputs), plan(inputs, root))
    assert catalog.snapshot() == before
    assert set(outcomes(again).values()) == {"already_imported"}
    materialize(inputs, root, "appended")
    grown = import_sessions(catalog, imports, grant(inputs), plan(inputs, root))
    export = imports.export(catalog, corpus)
    current_hits = imports.search(catalog, grant(inputs), "rollout cursor")
    assert {hit["source_generation"] for hit in current_hits} == {f"import:{outcomes_hash(grown)}"}
    retired = imports.retire(catalog, corpus, root / "export.json")
    assert json.loads((root / "export.json").read_text(encoding="utf-8"))["digest"] == export["digest"]
    assert imports.sessions(corpus) == [] and imports.search(catalog, grant(inputs), "cursor") == []
    assert len(catalog.snapshot()["session_event"]) == export["events"]
    return {
        "survived_crash": survived,
        "resumed": resumed,
        "rerun_outcomes": outcomes(again),
        "grown_source": grown,
        "export_manifest": {key: export[key] for key in ("digest", "sessions", "events")},
        "search_after_growth": current_hits,
        "retired": retired,
        "catalog_events_after_retire": len(catalog.snapshot()["session_event"]),
    }


CASES = {"a": positive, "b": rejection, "c": recovery}


def run_case(case):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as folder:
        observed = CASES[case](inputs, Path(folder))
    return {
        "case": f"T-SV2-SES-05-{case.upper()}",
        "state": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {FIXTURE.name: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
        "observed": observed,
    }
