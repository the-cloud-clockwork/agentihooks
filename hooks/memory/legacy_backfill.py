"""Idempotent backfill of legacy session history into the session corpus, one durable batch checkpoint at a time."""

import argparse
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from hooks.memory import source_audit as audit
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, Rejected, digest, ingest
from hooks.memory.catalog import Catalog
from hooks.memory.importer import PROVIDERS, read_source

IMPORTER_VERSION = "1"
METRIC = "legacy_import_remaining_records"
IDENTITY = ("provider", "installation", "native_session_id")
MATERIAL = ("raw", "raw_sha256", "preview", "vectors")
PREVIEW_KINDS = {"user": "user_text", "assistant": "assistant_text", "summary": "system_text"}
CONFLICT, REFUSED, IDENTITY_CONFLICT = "conflicting_source_content", "refused_source", "event_identity_conflict"
UNAVAILABLE = "dependency_unavailable"
STALE = "another runner moved this checkpoint; reopen the run and resume from its checkpoint"
PLAN_MISMATCH = "this run was opened for different sources; start a new run id"
PRAGMAS = ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL", "PRAGMA busy_timeout=5000")
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS legacy_backfill_run (
        run_id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL, batches_done INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS legacy_backfill_session (
        session_uid TEXT NOT NULL, source_generation TEXT NOT NULL, run_id TEXT NOT NULL, records INTEGER NOT NULL,
        provenance TEXT NOT NULL, active INTEGER NOT NULL, PRIMARY KEY (session_uid, source_generation))""",
    """CREATE TABLE IF NOT EXISTS legacy_backfill_quarantine (
        run_id TEXT NOT NULL, session_uid TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT NOT NULL,
        PRIMARY KEY (run_id, session_uid, reason, detail))""",
)
OPEN_RUN = "INSERT INTO legacy_backfill_run VALUES (?, ?, 0) ON CONFLICT (run_id) DO NOTHING"
READ_RUN = "SELECT plan_digest, batches_done FROM legacy_backfill_run WHERE run_id = ?"
ADVANCE = "UPDATE legacy_backfill_run SET batches_done = ? WHERE run_id = ? AND batches_done = ?"
MARK = "INSERT INTO legacy_backfill_session VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT DO NOTHING"
IMPORTED = "SELECT 1 FROM legacy_backfill_session WHERE session_uid = ? AND source_generation = ?"
QUARANTINE = "INSERT INTO legacy_backfill_quarantine VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING"
QUARANTINED = (
    "SELECT session_uid, reason, detail FROM legacy_backfill_quarantine WHERE run_id = ? "
    "ORDER BY session_uid, reason, detail"
)
SESSIONS = (
    "SELECT session_uid, source_generation, run_id, records, provenance, active FROM legacy_backfill_session "
    "WHERE run_id = ? ORDER BY session_uid, source_generation"
)
DEACTIVATE = "UPDATE legacy_backfill_session SET active = 0 WHERE run_id = ? AND active = 1"


@dataclass(frozen=True)
class BackfillPlan:
    corpus: str
    sources: tuple[dict, ...]
    aliases: Mapping[str, str]
    batch_size: int = 50


class BackfillLedger:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        for statement in (*PRAGMAS, *SCHEMA):
            self.db.execute(statement)

    def close(self) -> None:
        self.db.close()

    def open(self, run_id: str, plan_digest: str) -> int:
        self.db.execute(OPEN_RUN, (run_id, plan_digest))
        held, done = self.db.execute(READ_RUN, (run_id,)).fetchone()
        if held != plan_digest:
            raise Rejected("plan_mismatch", PLAN_MISMATCH)
        return done

    def checkpoint(self, run_id: str) -> int:
        row = self.db.execute(READ_RUN, (run_id,)).fetchone()
        return row[1] if row else 0

    def advance(self, run_id: str, expected: int, to: int) -> None:
        if self.db.execute(ADVANCE, (to, run_id, expected)).rowcount != 1:
            raise Rejected("stale_checkpoint", STALE)

    def imported(self, session_uid: str, generation: str) -> bool:
        return self.db.execute(IMPORTED, (session_uid, generation)).fetchone() is not None

    def mark(self, run_id: str, session_uid: str, generation: str, records: int, provenance: dict) -> None:
        self.db.execute(MARK, (session_uid, generation, run_id, records, json.dumps(provenance, sort_keys=True)))

    def quarantine(self, run_id: str, session_uid: str, reason: str, detail: str) -> None:
        self.db.execute(QUARANTINE, (run_id, session_uid, reason, detail))

    def quarantined(self, run_id: str) -> list[dict]:
        rows = self.db.execute(QUARANTINED, (run_id,)).fetchall()
        return [dict(zip(("session_uid", "reason", "detail"), row)) for row in rows]

    def sessions(self, run_id: str) -> list[dict]:
        names = ("session_uid", "source_generation", "run_id", "records", "provenance", "active")
        rows = self.db.execute(SESSIONS, (run_id,)).fetchall()
        return [{**dict(zip(names, row)), "provenance": json.loads(row[4]), "active": bool(row[5])} for row in rows]

    def deactivate(self, run_id: str) -> int:
        return self.db.execute(DEACTIVATE, (run_id,)).rowcount


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _source_error(index: int, source: object) -> str:
    if not isinstance(source, dict) or not all(_text(source.get(name)) for name in (*IDENTITY, "location")):
        return f"source {index} needs a provider, installation, native_session_id and location"
    if source["provider"] not in PROVIDERS:
        return f"source {index} provider is not claude or codex"
    return ""


def _plan_error(grant: Grant, plan: BackfillPlan, run_id: object) -> tuple[str, str] | None:
    if not _text(run_id):
        return "invalid_plan", "a run id must be a non-empty string"
    if not isinstance(plan.batch_size, int) or isinstance(plan.batch_size, bool) or plan.batch_size < 1:
        return "invalid_plan", "batch_size must be a positive integer"
    errors = [error for index, source in enumerate(plan.sources) if (error := _source_error(index, source))]
    if errors:
        return "invalid_plan", errors[0]
    if not all(
        PurePosixPath(folder).is_absolute() and ".." not in PurePosixPath(folder).parts for folder in plan.aliases
    ):
        return "invalid_plan", "alias folders must be absolute paths"
    if not set(plan.aliases.values()) <= grant.projects:
        return "forbidden_scope", "an alias names a project outside the grant"
    if plan.corpus != grant.corpus:
        return "forbidden_scope", "the plan is not approved for the granted corpus; nothing was imported"
    return None


def _observation(source: dict) -> dict:
    return {"source": source["location"], **{key: source[key] for key in MATERIAL if source.get(key) is not None}}


def _content_digest(found: dict) -> str | None:
    return found["raw_sha256"] or found["preview_sha256"] or found["vectors_sha256"]


def _audited(sources: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict]]:
    held, refused = [], []
    for source in sources:
        try:
            held.append((source, audit.audit_source(_observation(source))))
        except Rejected as error:
            refused.append({"reason": REFUSED, "detail": f"{source['location']}: {error.error_class}"})
    return held, refused


def _select(sources: list[dict]) -> dict:
    held, refused = _audited(sources)
    top = max((audit.RANK[found["class"]] for _, found in held), default=audit.RANK[audit.MISSING])
    best = [(source, found) for source, found in held if audit.RANK[found["class"]] == top]
    digests = sorted({_content_digest(found) for _, found in best} - {None})
    if len(digests) > 1:
        conflict = [{"reason": CONFLICT, "detail": found_digest} for found_digest in digests]
        return {"outcome": "quarantined", "quarantine": refused + conflict}
    if not digests:
        return {"outcome": "quarantined" if refused else "missing", "quarantine": refused}
    source, found = best[0]
    return {
        "outcome": "import",
        "quarantine": refused,
        "source": source,
        "found": found,
        "digest": digests[0],
        "references": sorted(source["location"] for source, _ in best),
    }


def _legacy_rows(source: dict, found: dict) -> list[dict]:
    if found["class"] == audit.PREVIEW_ONLY:
        return [
            {
                "kind": PREVIEW_KINDS.get(item["entry_type"]),
                "text": item["content"],
                "tool_names": item["tool_names"],
                "legacy_uuid": item["uuid"],
                "source_time": item["timestamp"] or None,
            }
            for item in source["preview"]
        ]
    return [
        {
            "kind": "meta",
            "text": row["text_preview"],
            "legacy_chunk_idx": row["chunk_idx"],
            "source_time": row["timestamp"] or None,
        }
        for row in sorted(source["vectors"], key=lambda row: row["chunk_idx"])
    ]


def _events(source: dict, found: dict, aliases: Mapping[str, str]) -> list[dict]:
    if found["class"] == audit.FULL_RAW:
        return read_source(source["provider"], source["raw"], aliases).events
    rows = _legacy_rows(source, found)
    return [
        {"sequence": sequence, "byte_start": 0, "byte_end": 0, "project_id": None, "record": record}
        for sequence, record in enumerate(rows, 1)
    ]


def _work(plan: BackfillPlan) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for source in plan.sources:
        groups.setdefault(tuple(source[name] for name in IDENTITY), []).append(source)
    items = []
    for key, sources in groups.items():
        item = {"key": dict(zip(IDENTITY, key)), "session_uid": ":".join((plan.corpus, *key)), **_select(sources)}
        if item["outcome"] == "import":
            raw = item["found"]["class"] == audit.FULL_RAW
            item["generation"] = f"{'import' if raw else 'legacy'}:{item['digest']}"
            item["events"] = _events(item["source"], item["found"], plan.aliases)
        items.append(item)
    return sorted(items, key=lambda item: item["session_uid"])


def _plan_digest(items: list[dict], batch_size: int) -> str:
    rows = [
        [item["session_uid"], item["outcome"], item.get("generation"), len(item.get("events", []))] for item in items
    ]
    return digest([batch_size, rows])


def provenance(item: dict, run_id: str) -> dict:
    events, found = item["events"], item["found"]
    times = [event["record"]["source_time"] for event in events if event["record"].get("source_time")]
    projects = [event["project_id"] for event in events]
    return {
        **item["key"],
        "source_references": item["references"],
        "fidelity": found["class"],
        "confidence": found["confidence"],
        "source_digest": item["digest"],
        "importer_version": IMPORTER_VERSION,
        "migration_run": run_id,
        "first_event_time": times[0] if times else None,
        "last_event_time": times[-1] if times else None,
        "verified_projects": sorted(set(projects) - {None}),
        "unknown_attribution_events": projects.count(None),
    }


class _Unavailable(Exception):
    pass


@dataclass(frozen=True)
class _Run:
    catalog: Catalog
    ledger: BackfillLedger
    grant: Grant
    run_id: str


def _request(run: _Run, item: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": item["generation"],
        "access_domain": run.grant.corpus,
        "session_uid": item["session_uid"],
        "source_generation": item["generation"],
        "events": item["events"],
    }


def _import(run: _Run, item: dict) -> str:
    uid, generation = item["session_uid"], item["generation"]
    if run.ledger.imported(uid, generation):
        return "already_imported"
    if not item["events"]:
        return "no_records"
    response = ingest(run.catalog, run.grant, _request(run, item))
    if response["state"] != "rejected":
        run.ledger.mark(run.run_id, uid, generation, len(item["events"]), provenance(item, run.run_id))
        return response["state"]
    error = response["error"]["class"]
    if error == UNAVAILABLE:
        raise _Unavailable
    run.ledger.quarantine(run.run_id, uid, IDENTITY_CONFLICT if error == "revision_conflict" else error, generation)
    return "quarantined"


def _session(run: _Run, item: dict) -> str:
    for entry in item["quarantine"]:
        run.ledger.quarantine(run.run_id, item["session_uid"], entry["reason"], entry["detail"])
    return _import(run, item) if item["outcome"] == "import" else item["outcome"]


def _batches(run: _Run, items: list[dict], size: int, limit: int | None) -> tuple[str, list[dict]]:
    handled: list[dict] = []
    batches = [items[start : start + size] for start in range(0, len(items), size)]
    for index in range(run.ledger.checkpoint(run.run_id), len(batches)):
        for item in batches[index]:
            if limit is not None and len(handled) >= limit:
                return "partial", handled
            try:
                outcome = _session(run, item)
            except _Unavailable:
                return "interrupted", handled
            handled.append({"session_uid": item["session_uid"], "outcome": outcome})
        run.ledger.advance(run.run_id, index, index + 1)
    return "completed", handled


def _remaining(ledger: BackfillLedger, items: list[dict]) -> list[dict]:
    return [
        item
        for item in items
        if item["outcome"] == "import"
        and item["events"]
        and not ledger.imported(item["session_uid"], item["generation"])
    ]


def _report(run: _Run, items: list[dict], size: int, state: str, handled: list[dict]) -> dict:
    left = _remaining(run.ledger, items)
    planned = [item for item in items if item["outcome"] == "import"]
    return {
        "state": state,
        "run_id": run.run_id,
        "importer_version": IMPORTER_VERSION,
        "principal": run.grant.principal,
        "batches": {"done": run.ledger.checkpoint(run.run_id), "total": -(-len(items) // size)},
        "sessions": handled,
        METRIC: sum(len(item["events"]) for item in left),
        "remaining_sessions": [item["session_uid"] for item in left],
        "missing": [item["session_uid"] for item in items if item["outcome"] == "missing"],
        "quarantined": run.ledger.quarantined(run.run_id),
        "verified_alias_events": sum(event["project_id"] is not None for item in planned for event in item["events"]),
        "unknown_attribution_events": sum(event["project_id"] is None for item in planned for event in item["events"]),
    }


def backfill(
    catalog: Catalog, ledger: BackfillLedger, grant: Grant, plan: BackfillPlan, run_id: str, limit: int | None = None
) -> dict:
    error = _plan_error(grant, plan, run_id)
    if error:
        return {"state": "rejected", "error": {"class": error[0], "detail": error[1]}}
    items = _work(plan)
    run = _Run(catalog, ledger, grant, run_id)
    try:
        ledger.open(run_id, _plan_digest(items, plan.batch_size))
        state, handled = _batches(run, items, plan.batch_size, limit)
    except Rejected as refusal:
        return {"state": "rejected", "error": {"class": refusal.error_class, "detail": refusal.detail}}
    return _report(run, items, plan.batch_size, state, handled)


def load_plan(path: Path) -> BackfillPlan:
    doc = json.loads(path.read_bytes())
    sources = tuple(
        {**source, "raw": (path.parent / source["raw"]).read_bytes()}
        if isinstance(source, dict) and isinstance(source.get("raw"), str)
        else source
        for source in doc["sources"]
    )
    return BackfillPlan(doc["corpus"], sources, doc.get("aliases", {}), doc.get("batch_size", 50))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("catalog", type=Path)
    run.add_argument("ledger", type=Path)
    run.add_argument("plan", type=Path)
    run.add_argument("run_id")
    run.add_argument("--corpus", required=True)
    run.add_argument("--project", action="append", default=[])
    run.add_argument("--principal", default="operator")
    run.add_argument("--limit", type=int)
    for name in ("deactivate", "report"):
        command = commands.add_parser(name)
        command.add_argument("ledger", type=Path)
        command.add_argument("run_id")
    args = parser.parse_args(argv)
    ledger = BackfillLedger(args.ledger)
    try:
        if args.command == "run":
            grant = Grant(args.principal, args.corpus, frozenset(args.project))
            with closing(Catalog(args.catalog)) as catalog:
                result = backfill(catalog, ledger, grant, load_plan(args.plan), args.run_id, args.limit)
        elif args.command == "deactivate":
            result = {"run_id": args.run_id, "deactivated": ledger.deactivate(args.run_id)}
        else:
            result = {
                "run_id": args.run_id,
                "checkpoint": ledger.checkpoint(args.run_id),
                "sessions": ledger.sessions(args.run_id),
                "quarantined": ledger.quarantined(args.run_id),
            }
    finally:
        ledger.close()
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get("state") == "rejected" else 0


if __name__ == "__main__":
    raise SystemExit(main())
