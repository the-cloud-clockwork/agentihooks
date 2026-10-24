"""Historical session backfill: approved provider roots are read once and committed through central ingestion.

A source is marked imported only after ingestion accepted it, so a rerun skips it by source hash and a source
committed before its mark is replayed by the catalog without a second copy. A project comes only from an explicit
absolute folder alias; a matching basename never attributes an event, and unknown attribution stays null.
"""

import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, digest, ingest
from hooks.memory.catalog import Catalog
from hooks.memory.records import TurnState, normalize_entries

IMPORTER_VERSION = "1"
PROVIDERS = ("claude", "codex")
UNKNOWN_ATTRIBUTION = "historical_import_unknown_attribution"
ACCEPTED = ("committed", "replayed", "would_import")
SEARCHED = ("text", "tool_name", "tool_input", "tool_result")
OPTIONAL_METADATA = ("provider_version", "first_event_time", "last_event_time")
SOURCE_FIELDS = ("provider", "installation", "relative_path", "source_sha256", "importer_version", "unknown")
HIT_FIELDS = ("event_id", "sequence", "byte_start", "byte_end", "project_id", "record")
PRAGMAS = ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL", "PRAGMA busy_timeout=5000")
SCHEMA = """CREATE TABLE IF NOT EXISTS session_import (
    access_domain TEXT NOT NULL, source_hash TEXT NOT NULL, session_uid TEXT NOT NULL,
    source_generation TEXT NOT NULL, provenance TEXT NOT NULL, unknown_events INTEGER NOT NULL,
    source_key TEXT NOT NULL, PRIMARY KEY (access_domain, source_hash))"""
MARK = "INSERT INTO session_import VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (access_domain, source_hash) DO NOTHING"
CURRENT = (
    "SELECT session_uid, source_generation, provenance FROM session_import AS row WHERE access_domain = ? "
    "AND rowid = (SELECT MAX(rowid) FROM session_import WHERE access_domain = row.access_domain "
    "AND source_key = row.source_key) ORDER BY session_uid, source_generation"
)
KNOWN = "SELECT session_uid FROM session_import WHERE access_domain = ? AND source_hash = ?"
SESSIONS = (
    "SELECT session_uid, source_generation, provenance FROM session_import WHERE access_domain = ? "
    "ORDER BY session_uid, source_generation"
)
UNKNOWN_EVENTS = "SELECT TOTAL(unknown_events) FROM session_import WHERE access_domain = ?"
RETIRE = "DELETE FROM session_import WHERE access_domain = ?"
ALL_ROWS = "SELECT * FROM session_import"
MISMATCH = "the written export does not match the current import projections; nothing was retired"
EXISTS = "the export destination already exists; nothing was retired"


@dataclass(frozen=True)
class SourceRoot:
    provider: str
    installation: str
    path: Path
    corpus: str
    expected: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportPlan:
    roots: tuple[SourceRoot, ...]
    aliases: Mapping[str, str]


@dataclass(frozen=True)
class SourceRead:
    events: list[dict]
    losses: list[dict]
    metadata: dict


def _absolute(folder: str) -> bool:
    path = PurePosixPath(folder)
    return path.is_absolute() and ".." not in path.parts


def resolve_project(cwd: object, aliases: Mapping[str, str]) -> str | None:
    if not isinstance(cwd, str) or not _absolute(cwd):
        return None
    matches = [folder for folder in aliases if PurePosixPath(cwd).is_relative_to(folder)]
    return aliases[max(matches, key=len)] if matches else None


def _observed(provider: str, entry: dict) -> tuple[object, object, object]:
    if provider == "claude":
        return entry.get("sessionId"), entry.get("version"), entry.get("cwd")
    payload = entry.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    kind = entry.get("type")
    cwd = payload.get("cwd") if kind in ("session_meta", "turn_context") else None
    if kind != "session_meta":
        return None, None, cwd
    return payload.get("id"), payload.get("cli_version"), cwd


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _observe(provider: str, entry: dict, metadata: dict) -> str | None:
    session, version, cwd = (_text(value) for value in _observed(provider, entry))
    metadata["native_session_id"] = metadata["native_session_id"] or session
    metadata["provider_version"] = metadata["provider_version"] or version
    if cwd and cwd not in metadata["working_folders"]:
        metadata["working_folders"].append(cwd)
    time = _text(entry.get("timestamp"))
    if time:
        metadata["first_event_time"] = metadata["first_event_time"] or time
        metadata["last_event_time"] = time
    return cwd


def _entry(piece: bytes) -> dict | None:
    try:
        entry = json.loads(piece.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return entry if isinstance(entry, dict) else None


def _events(provider: str, entry: dict, turn: TurnState, first: int, base: dict) -> list[dict]:
    time = _text(entry.get("timestamp"))
    return [
        {
            **base,
            "sequence": first + index,
            "record": {**{key: value for key, value in record.items() if key != "raw"}, "source_time": time},
        }
        for index, record in enumerate(normalize_entries(provider, [entry], turn))
    ]


def read_source(provider: str, data: bytes, aliases: Mapping[str, str]) -> SourceRead:
    metadata = dict.fromkeys(("native_session_id", "provider_version", "first_event_time", "last_event_time"))
    metadata["working_folders"] = []
    events, losses, turn, cwd, offset = [], [], TurnState(), None, 0
    pieces = data.split(b"\n")
    for index, piece in enumerate(pieces):
        span = {"byte_start": offset, "byte_end": offset + len(piece)}
        offset += len(piece) + 1
        if not piece.strip():
            continue
        entry = _entry(piece)
        if entry is None:
            reason = "truncated final line" if index == len(pieces) - 1 else "unparseable line"
            losses.append({**span, "reason": reason})
            continue
        cwd = _observe(provider, entry, metadata) or cwd
        events += _events(provider, entry, turn, len(events) + 1, {**span, "project_id": resolve_project(cwd, aliases)})
    return SourceRead(events, losses, metadata)


def _ambiguous(folders: list[str], aliases: Mapping[str, str]) -> list[str]:
    names = {PurePosixPath(folder).name for folder in aliases}
    return sorted(
        folder for folder in folders if resolve_project(folder, aliases) is None and PurePosixPath(folder).name in names
    )


def _provenance(root: SourceRoot, relative: str, data: bytes, read: SourceRead, aliases: Mapping[str, str]) -> dict:
    unknown_events = sum(event["project_id"] is None for event in read.events)
    named = read.metadata["native_session_id"] is not None
    provenance = {
        "provider": root.provider,
        "installation": root.installation,
        "relative_path": relative,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "source_bytes": len(data),
        "importer_version": IMPORTER_VERSION,
        **read.metadata,
        "native_session_id": read.metadata["native_session_id"] if named else PurePosixPath(relative).stem,
        "session_id_source": "transcript" if named else "file name",
        "projects": sorted({event["project_id"] for event in read.events} - {None}),
        "unknown_attribution_events": unknown_events,
        "ambiguous_basenames": _ambiguous(read.metadata["working_folders"], aliases),
        "losses": read.losses,
    }
    unknown = [name for name in OPTIONAL_METADATA if provenance[name] is None]
    return {**provenance, "unknown": unknown + ["project"] * bool(unknown_events)}


def ingest_request(domain: str, session_uid: str, source_hash: str, events: list[dict]) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": f"import:{source_hash}",
        "access_domain": domain,
        "session_uid": session_uid,
        "source_generation": f"import:{source_hash}",
        "events": events,
    }


class ImportLedger:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        for statement in (*PRAGMAS, SCHEMA):
            self.db.execute(statement)

    def close(self) -> None:
        self.db.close()

    def session_uid(self, domain: str, source_hash: str) -> str | None:
        row = self.db.execute(KNOWN, (domain, source_hash)).fetchone()
        return row[0] if row else None

    def has(self, domain: str, source_hash: str) -> bool:
        return self.session_uid(domain, source_hash) is not None

    def mark(self, domain: str, session_uid: str, provenance: dict) -> None:
        source_hash = provenance["source_sha256"]
        row = (domain, source_hash, session_uid, f"import:{source_hash}", json.dumps(provenance))
        key = json.dumps([provenance[name] for name in ("provider", "installation", "relative_path")])
        self.db.execute(MARK, (*row, provenance["unknown_attribution_events"], key))

    def sessions(self, domain: str, current: bool = False) -> list[dict]:
        rows = self.db.execute(CURRENT if current else SESSIONS, (domain,)).fetchall()
        return [{"session_uid": row[0], "source_generation": row[1], "provenance": json.loads(row[2])} for row in rows]

    def unknown_attribution(self, domain: str) -> int:
        return int(self.db.execute(UNKNOWN_EVENTS, (domain,)).fetchone()[0])

    def snapshot(self) -> list:
        return sorted(self.db.execute(ALL_ROWS).fetchall())

    def search(self, catalog: Catalog, grant: Grant, text: str) -> list[dict]:
        domain, needle = grant.corpus, text.casefold()
        return [
            {
                "session_uid": session["session_uid"],
                "source_generation": session["source_generation"],
                **{key: event[key] for key in HIT_FIELDS},
                "source": {key: session["provenance"][key] for key in SOURCE_FIELDS},
            }
            for session in self.sessions(domain, current=True)
            for event in catalog.events(domain, session["session_uid"], session["source_generation"])
            if event["project_id"] is None or event["project_id"] in grant.projects
            if needle in " ".join(str(event["record"][key]) for key in SEARCHED if key in event["record"]).casefold()
        ]

    def export(self, catalog: Catalog, domain: str) -> dict:
        records = [
            {**session, "events": catalog.events(domain, session["session_uid"], session["source_generation"])}
            for session in self.sessions(domain)
        ]
        return {
            "access_domain": domain,
            "records": records,
            "digest": digest(records),
            "sessions": len(records),
            "events": sum(len(record["events"]) for record in records),
        }

    def retire(self, catalog: Catalog, domain: str, destination: Path) -> dict:
        if destination.exists():
            return {"state": "rejected", "error": {"class": "export_exists", "detail": EXISTS}}
        current = self.export(catalog, domain)
        _write_durably(destination, json.dumps(current, ensure_ascii=False, sort_keys=True))
        if digest(json.loads(destination.read_text(encoding="utf-8"))["records"]) != current["digest"]:
            return {"state": "rejected", "error": {"class": "export_mismatch", "detail": MISMATCH}}
        self.db.execute(RETIRE, (domain,))
        manifest = {key: current[key] for key in ("digest", "sessions", "events")}
        return {"state": "retired", "catalog_events_kept": current["events"], "export": manifest}


def _write_durably(destination: Path, text: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "x", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _plan_error(grant: Grant, plan: ImportPlan) -> tuple[str, str] | None:
    if not plan.roots:
        return "invalid_plan", "a plan must name at least one source root"
    for index, root in enumerate(plan.roots):
        if root.provider not in PROVIDERS:
            return "invalid_plan", f"root {index} provider is not claude or codex"
    if not all(_absolute(folder) for folder in plan.aliases):
        return "invalid_plan", "alias folders must be absolute paths"
    if not set(plan.aliases.values()) <= grant.projects:
        return "forbidden_scope", "an alias names a project outside the grant"
    for index, root in enumerate(plan.roots):
        if root.corpus != grant.corpus:
            return "forbidden_scope", f"root {index} is not approved for the granted corpus; nothing was imported"
    return None


def _discover(folder: Path) -> list[Path]:
    inside = folder.resolve()
    return sorted(
        path
        for path in folder.rglob("*.jsonl")
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(inside)
    )


@dataclass(frozen=True)
class _Run:
    catalog: Catalog
    imports: ImportLedger
    grant: Grant
    plan: ImportPlan
    dry_run: bool
    report: dict


def _label(root: SourceRoot, relative: str) -> dict:
    return {"provider": root.provider, "installation": root.installation, "relative_path": relative}


def _commit(run: _Run, summary: dict, provenance: dict, events: list[dict]) -> dict:
    if not events:
        return {**summary, "outcome": "no_records"}
    if run.dry_run:
        return {**summary, "outcome": "would_import"}
    request = ingest_request(run.grant.corpus, summary["session_uid"], provenance["source_sha256"], events)
    response = ingest(run.catalog, run.grant, request)
    if response["state"] == "rejected":
        return {**summary, "outcome": "refused", "error": response["error"]["class"]}
    run.imports.mark(run.grant.corpus, summary["session_uid"], provenance)
    return {**summary, "outcome": response["state"]}


def _import_source(run: _Run, root: SourceRoot, path: Path) -> None:
    label = _label(root, path.relative_to(root.path).as_posix())
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        run.report["missing_sources"].append({**label, "reason": "source vanished before it was read"})
        return
    source_hash = hashlib.sha256(data).hexdigest()
    known = run.imports.session_uid(run.grant.corpus, source_hash)
    summary = {**label, "source_sha256": source_hash, "outcome": "already_imported", "session_uid": known}
    if known:
        run.report["sources"].append({**summary, "events": 0, "unknown_attribution_events": 0})
        return
    read = read_source(root.provider, data, run.plan.aliases)
    run.report["losses"] += [{**label, **loss} for loss in read.losses]
    provenance = _provenance(root, label["relative_path"], data, read, run.plan.aliases)
    summary |= {
        "session_uid": f"{run.grant.corpus}:{root.provider}:{root.installation}:{provenance['native_session_id']}",
        "events": len(read.events),
        "unknown_attribution_events": provenance["unknown_attribution_events"],
    }
    outcome = _commit(run, summary, provenance, read.events)
    if outcome["outcome"] in ACCEPTED:
        run.report[UNKNOWN_ATTRIBUTION] += outcome["unknown_attribution_events"]
    run.report["sources"].append(outcome)


def _import_root(run: _Run, root: SourceRoot) -> None:
    if not root.path.is_dir():
        run.report["missing_roots"].append({"provider": root.provider, "installation": root.installation})
        return
    for path in _discover(root.path):
        _import_source(run, root, path)
    for relative in root.expected:
        if not (root.path / relative).is_file():
            run.report["missing_sources"].append({**_label(root, relative), "reason": "expected source is absent"})


def import_sessions(
    catalog: Catalog, imports: ImportLedger, grant: Grant, plan: ImportPlan, dry_run: bool = False
) -> dict:
    error = _plan_error(grant, plan)
    if error:
        return {"state": "rejected", "error": {"class": error[0], "detail": error[1]}}
    report = {
        "state": "completed",
        "dry_run": dry_run,
        "importer_version": IMPORTER_VERSION,
        "sources": [],
        "missing_roots": [],
        "missing_sources": [],
        "losses": [],
        UNKNOWN_ATTRIBUTION: 0,
    }
    run = _Run(catalog, imports, grant, plan, dry_run, report)
    for root in plan.roots:
        _import_root(run, root)
    return report
