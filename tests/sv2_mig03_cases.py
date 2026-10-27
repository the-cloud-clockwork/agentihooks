import json
import sqlite3
from pathlib import Path

from hooks.memory import legacy_backfill as backfill
from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import Catalog
from hooks.memory.importer import ImportLedger, ImportPlan, SourceRoot, import_sessions

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sv2_mig03"
FIXTURE = FIXTURE_DIR / "fixture.json"
RAW_FILES = tuple(sorted(FIXTURE_DIR.rglob("*.jsonl")))
CASES_MODULE = Path(__file__)
FIXTURE_INPUTS = {
    "duplicate_source_directories": "the laptop s-shared transcript copied byte for byte into two folders, beside a "
    "lower fidelity legacy preview of the same session",
    "equal_native_ids_from_different_machines": "s-shared on the laptop and s-shared on the desktop with different "
    "content, kept apart by the installation namespace",
    "mid_batch_interruption": "a run stopped after three sessions inside its second batch of two, and a run stopped by "
    "a catalog outage, both resumed at the durable batch checkpoint",
    "conflicts": "s-conflict held in two folders with different bytes, s-forged whose bytes do not match its recorded "
    "digest, and a catalog already holding a different event at a backfill event identity",
    "fidelity": "s-preview survives only as a legacy preview, s-vector only as vector rows, s-missing as nothing",
}
EVIDENCE_CLASS = (
    "mocked: the legacy previews, vector rows and transcript folders are fixture data shaped like the retired "
    "transcript service's stores, and temporary SQLite files stand in for the session catalog and backfill ledger; "
    "no live corpus, legacy Redis or workstation folder was read or written"
)
RUN = "migration-1"
TABLES = (
    "session_event",
    "session_quarantine",
    "session_ingest_batch",
    "legacy_backfill_run",
    "legacy_backfill_session",
    "legacy_backfill_quarantine",
)


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def plan() -> backfill.BackfillPlan:
    return backfill.load_plan(FIXTURE)


def grant(doc: dict) -> Grant:
    return Grant(doc["grant"]["principal"], doc["corpus"], frozenset(doc["grant"]["projects"]))


def stores(root: Path) -> tuple[Catalog, backfill.BackfillLedger]:
    root.mkdir(parents=True, exist_ok=True)
    return Catalog(root / "catalog.db"), backfill.BackfillLedger(root / "backfill.db")


def tables(root: Path) -> dict[str, list]:
    found = {}
    for name in ("catalog.db", "backfill.db"):
        db = sqlite3.connect(root / name)
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        found |= {table: sorted(db.execute(f"SELECT * FROM {table}").fetchall()) for table in TABLES if table in names}
        db.close()
    return found


def events(catalog: Catalog, corpus: str) -> list[list]:
    rows = catalog.db.execute(
        "SELECT session_uid, source_generation, sequence, record FROM session_event WHERE access_domain = ? "
        "ORDER BY session_uid, sequence",
        (corpus,),
    ).fetchall()
    return [
        [uid, generation, sequence, json.loads(record).get("source_time")] for uid, generation, sequence, record in rows
    ]


def summary(report: dict) -> dict:
    keep = ("state", "batches", "sessions", backfill.METRIC, "remaining_sessions", "missing", "quarantined")
    return {key: report[key] for key in (*keep, "verified_alias_events", "unknown_attribution_events") if key in report}


def earlier_importer(catalog: Catalog, root: Path, doc: dict) -> dict:
    imports = ImportLedger(root / "imports.db")
    source = SourceRoot("claude", "desktop", FIXTURE_DIR / "desktop", doc["corpus"])
    report = import_sessions(catalog, imports, grant(doc), ImportPlan((source,), doc["aliases"]))
    imports.close()
    return {"state": report["state"], "outcomes": [item["outcome"] for item in report["sources"]]}


def independent_pass(root: Path) -> dict:
    doc = fixture()
    catalog, ledger = stores(root)
    importer = earlier_importer(catalog, root, doc)
    first = backfill.backfill(catalog, ledger, grant(doc), plan(), RUN)
    before = events(catalog, doc["corpus"])
    second = backfill.backfill(catalog, ledger, grant(doc), plan(), "migration-2")
    result = {
        "earlier_importer": importer,
        "first": summary(first),
        "second": summary(second),
        "events": before,
        "second_pass_changed_events": events(catalog, doc["corpus"]) != before,
        "duplicate_events_total": catalog.duplicate_events_total(doc["corpus"]),
        "projections": ledger.sessions(RUN),
        "second_run_projections": ledger.sessions("migration-2"),
    }
    catalog.close()
    ledger.close()
    return result


def positive(root: Path) -> dict:
    first, repeat = independent_pass(root / "first"), independent_pass(root / "second")
    return {"case": "A", **first, "repeat_matches": first == repeat}


def conflicting_catalog(root: Path, doc: dict) -> Catalog:
    catalog, ledger = stores(root)
    ledger.close()
    item = next(item for item in backfill._work(plan()) if item["key"]["native_session_id"] == "s-preview")
    forged = {**item["events"][0], "record": {**item["events"][0]["record"], "text": "a different earlier write"}}
    request = {
        "schema_version": backfill.SCHEMA_VERSION,
        "batch_id": "an-earlier-writer",
        "access_domain": doc["corpus"],
        "session_uid": item["session_uid"],
        "source_generation": item["generation"],
        "events": [forged],
    }
    backfill.ingest(catalog, grant(doc), request)
    return catalog


def refusal(root: Path, doc: dict, the_grant: Grant, the_plan: backfill.BackfillPlan, run_id: object) -> dict:
    before = tables(root)
    catalog, ledger = stores(root)
    report = backfill.backfill(catalog, ledger, the_grant, the_plan, run_id)
    catalog.close()
    ledger.close()
    return {"report": report, "unchanged": tables(root) == before}


def rejection(root: Path) -> dict:
    doc = fixture()
    catalog = conflicting_catalog(root, doc)
    ledger = backfill.BackfillLedger(root / "backfill.db")
    report = backfill.backfill(catalog, ledger, grant(doc), plan(), RUN)
    preview_uid = "engineering:claude:laptop:s-preview"
    protected = catalog.events(doc["corpus"], preview_uid, generation_of(preview_uid))
    catalog.close()
    ledger.close()
    wide = Grant("operator", doc["corpus"], frozenset())
    other = Grant("operator", "another-corpus", frozenset(doc["grant"]["projects"]))
    refusals = {
        "alias_outside_grant": refusal(root, doc, wide, plan(), "migration-3"),
        "corpus_outside_grant": refusal(root, doc, other, plan(), "migration-3"),
        "changed_sources_under_one_run": refusal(
            root, doc, grant(doc), backfill.BackfillPlan(doc["corpus"], plan().sources[:-1], doc["aliases"], 2), RUN
        ),
    }
    return {
        "case": "B",
        "report": summary(report),
        "protected_event_kept": [event["record"]["text"] for event in protected],
        "refusals": refusals,
        "refusals_disclose_content": doc["private_label"] in json.dumps(refusals),
        "quarantine_disclose_content": doc["private_label"] in json.dumps(report["quarantined"]),
    }


def generation_of(uid: str) -> str:
    return next(item["generation"] for item in backfill._work(plan()) if item["session_uid"] == uid)


class OutageCatalog(Catalog):
    def commit(self, batch):
        raise sqlite3.OperationalError("database is locked")


def recovery(root: Path) -> dict:
    doc = fixture()
    catalog, ledger = stores(root)
    stopped = backfill.backfill(catalog, ledger, grant(doc), plan(), RUN, limit=3)
    catalog.close()
    ledger.close()
    survived = tables(root)
    outage = OutageCatalog(root / "catalog.db")
    stale_runner = backfill.BackfillLedger(root / "backfill.db")
    held_checkpoint = stale_runner.checkpoint(RUN)
    interrupted = backfill.backfill(outage, stale_runner, grant(doc), plan(), RUN)
    outage.close()
    catalog, ledger = stores(root)
    resumed = backfill.backfill(catalog, ledger, grant(doc), plan(), RUN)
    try:
        stale_runner.advance(RUN, held_checkpoint, held_checkpoint + 1)
        stale = "accepted"
    except backfill.Rejected as error:
        stale = error.error_class
    replay = backfill.backfill(catalog, ledger, grant(doc), plan(), RUN)
    deactivated = ledger.deactivate(RUN)
    result = {
        "case": "C",
        "stopped": summary(stopped),
        "survived_restart": {table: len(rows) for table, rows in survived.items()},
        "interrupted_by_outage": summary(interrupted),
        "resumed": summary(resumed),
        "stale_runner_checkpoint": held_checkpoint,
        "stale_runner_advance": stale,
        "replay_of_completed_run": summary(replay),
        "events": events(catalog, doc["corpus"]),
        "duplicate_events_total": catalog.duplicate_events_total(doc["corpus"]),
        "rollback": {
            "deactivated": deactivated,
            "active_after": [row["active"] for row in ledger.sessions(RUN)],
            "catalog_events_kept": len(events(catalog, doc["corpus"])),
            "sources_kept": all(path.is_file() for path in RAW_FILES),
        },
    }
    catalog.close()
    ledger.close()
    stale_runner.close()
    return result


def run_case(case: str, root: Path) -> dict:
    return {"A": positive, "B": rejection, "C": recovery}[case](root)
