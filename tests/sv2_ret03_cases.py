import hashlib
import json
import sqlite3
from pathlib import Path

from hooks.memory import catalog as catalog_module
from hooks.memory import migrations
from hooks.memory.catalog import BatchCommit, Catalog, Conflict, SessionSource, StoredEvent
from hooks.memory.embeddings import EmbeddingModel
from hooks.memory.index_worker import SAVE_VECTOR, IndexWorker

FIXTURE = Path(__file__).parent / "fixtures" / "sv2_ret03" / "fixture.json"
FIXTURE_INPUTS = {
    "previous_schema": "a catalog file created without the event provenance table, written by the previous positional writer",
    "new_optional_fields": "trust class and project state per event, written by the new writer and backfilled for older rows",
    "interrupted_backfill": "a backfill that stops inside its third batch and resumes after a restart",
    "mismatched_vector_dimension": "two vectors of dimension four under an index generation registered at dimension three",
}
PREVIOUS_WRITER = "INSERT INTO session_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
PREVIOUS_READER = "SELECT * FROM session_event ORDER BY access_domain, session_uid, source_generation, sequence"
EVENT_IDS = "SELECT event_id, session_uid, sequence FROM session_event ORDER BY session_uid, sequence"
PROTECTED = ("session_event", "session_source", "session_record", "chunk_vector", "embedding_work", "index_generation")


class Interrupted(OSError):
    pass


class Interrupting(migrations.Migrator):
    def __init__(self, catalog, fills):
        super().__init__(catalog)
        self.fills = fills

    def _fill(self, after, through):
        filled = super()._fill(after, through)
        if self.fills == 0:
            raise Interrupted("the backfill process stopped inside a batch")
        self.fills -= 1
        return filled


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def event(uid: str, sequence: int, project_id: str | None) -> StoredEvent:
    record = {"kind": "user_text", "session": uid, "sequence": sequence}
    digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    return StoredEvent(f"{uid}-e{sequence}", sequence, sequence * 100, sequence * 100 + 99, project_id, record, digest)


class World:
    def __init__(self, root: Path):
        self.doc, self.path = fixture(), root / "catalog.sqlite3"
        self.domain = self.doc["domain"]
        root.mkdir(parents=True, exist_ok=True)
        self.projects = {item["session_uid"]: item["project_id"] for item in self.doc["sessions"]}
        previous = sqlite3.connect(self.path)
        for statement in catalog_module.SCHEMA:
            if "session_event_provenance" not in statement:
                previous.execute(statement)
        previous.commit()
        previous.close()
        self.open()
        index = self.doc["index"]
        self.index = IndexWorker(self.catalog)
        self.index.register(self.domain, index["generation"], EmbeddingModel(index["model"], index["dimension"]))
        for item in self.doc["sessions"]:
            source = (item["session_uid"], item["source_generation"], item["provider"], item["origin"])
            self.catalog.register(SessionSource(self.domain, *source, "retain-until-deleted"))
            self.previous_write(item["session_uid"], item["source_generation"], range(1, item["events"] + 1))
        for vector in self.doc["vectors"]:
            self.save_vector(vector)

    def open(self):
        self.catalog = Catalog(self.path)
        self.migrator = migrations.Migrator(self.catalog)

    def restart(self):
        self.catalog.close()
        self.open()

    def previous_write(self, uid: str, generation: str, sequences) -> None:
        for sequence in sequences:
            found = event(uid, sequence, self.projects[uid])
            row = (self.domain, found.event_id, uid, generation, sequence, found.byte_start, found.byte_end)
            self.catalog.db.execute(
                PREVIOUS_WRITER, (*row, found.project_id, json.dumps(found.record), found.normalized_hash)
            )

    def new_write(self, uid: str, generation: str, sequences) -> dict:
        events = [event(uid, sequence, self.projects[uid]) for sequence in sequences]
        batch = BatchCommit(self.domain, f"batch-{uid}-{sequences[0]}", "digest", uid, generation, events, [], "1.0")
        return self.catalog.commit(batch)

    def save_vector(self, vector: dict) -> None:
        index = self.doc["index"]
        identity = (vector["session_uid"], index["chunker_version"], f"hash-{vector['chunk_id']}")
        values = [0.5] * vector["dimension"]
        row = (self.domain, index["generation"], vector["chunk_id"], *identity, index["model"], vector["dimension"])
        self.catalog.db.execute(SAVE_VECTOR, (*row, json.dumps(values)))

    def window(self) -> dict:
        new, old = self.doc["window"]["new_writer"], self.doc["window"]["previous_writer"]
        start = {item["session_uid"]: item["events"] for item in self.doc["sessions"]}
        committed = self.new_write(
            new["session_uid"],
            new["source_generation"],
            list(range(start[new["session_uid"]] + 1, start[new["session_uid"]] + new["events"] + 1)),
        )
        first = start[old["session_uid"]] + 1
        self.previous_write(old["session_uid"], old["source_generation"], range(first, first + old["events"]))
        return {"new_writer_state": committed["state"], "new_writer_event_ids": committed["accepted"]["event_ids"]}

    def backfill(self, migrator=None) -> dict:
        plan = self.doc["backfill"]
        migrator = migrator or self.migrator
        return migrator.backfill(migrator.status()["revision"], plan["batch_size"], plan["max_batches"])

    def staged(self, name: str) -> dict:
        return getattr(self.migrator, name)(self.migrator.status()["revision"])

    def protected(self) -> dict:
        return {table: sorted(self.catalog.db.execute(f"SELECT * FROM {table}").fetchall()) for table in PROTECTED}

    def identities(self) -> list:
        return [list(row) for row in self.catalog.db.execute(EVENT_IDS).fetchall()]

    def previous_reader(self) -> dict:
        rows = self.catalog.db.execute(PREVIOUS_READER).fetchall()
        return {"columns": len(rows[0]), "rows": len(rows)}

    def events(self) -> dict:
        return {
            item["session_uid"]: [
                (found["event_id"], found["project_id"])
                for found in self.catalog.events(self.domain, item["session_uid"], item["source_generation"])
            ]
            for item in self.doc["sessions"]
        }

    def provenance(self) -> dict:
        return {uid: self.migrator.provenance(self.domain, f"{uid}-e1") for uid in sorted(self.projects)}

    def vectors(self) -> list:
        query = "SELECT chunk_id, dimension FROM chunk_vector ORDER BY chunk_id"
        return [list(row) for row in self.catalog.db.execute(query).fetchall()]

    def work(self) -> list:
        query = "SELECT chunk_id, queued_at FROM embedding_work ORDER BY chunk_id"
        return [list(row) for row in self.catalog.db.execute(query).fetchall()]

    def approval(self, **fields) -> migrations.Approval:
        base = {
            **self.doc["operator"],
            "migration_id": migrations.DIMENSION,
            "access_domain": self.domain,
            "index_generation": self.doc["index"]["generation"],
        }
        return migrations.Approval(**{**base, **fields})


def stage(status: dict) -> dict:
    keys = ("state", "after_rowid", "rows_done", "migration_rows_remaining")
    return {key: status[key] for key in keys}


def refused(call) -> str:
    try:
        call()
    except Conflict as error:
        return str(error)
    return "accepted"


def migrate(root: Path) -> dict:
    world = World(root)
    before = {"events": world.events(), "previous_reader": world.previous_reader(), "vectors": world.vectors()}
    before_rows, previous_rows = before["events"], world.catalog.db.execute(PREVIOUS_READER).fetchall()
    started = world.migrator.startup()
    window = world.window()
    after_window = world.migrator.status()["migration_rows_remaining"]
    backfilled = world.backfill()
    verified = world.staged("verify")
    active = world.staged("activate")
    after = world.events()
    unchanged = all(after[uid][: len(rows)] == rows for uid, rows in before_rows.items())
    after_rows = world.catalog.db.execute(PREVIOUS_READER).fetchall()
    late = world.doc["window"]["previous_writer"]
    sequence = len(after["s-unattributed"]) + 1
    world.previous_write(late["session_uid"], late["source_generation"], range(sequence, sequence + 1))
    late_id = f"{late['session_uid']}-e{sequence}"
    return {
        "before": before,
        "startup": {"added": started["added"], "status": stage(started["status"]), "blocked": started["blocked"]},
        "window": {**window, "migration_rows_remaining": after_window},
        "backfill": stage(backfilled),
        "verification": verified["report"],
        "activated": active["state"],
        "provenance": world.provenance(),
        "previous_reader_after": world.previous_reader(),
        "previous_events_unchanged": unchanged,
        "previous_rows_unchanged": all(row in after_rows for row in previous_rows),
        "previous_writer_after_activation": {
            "event_id": late_id,
            "provenance": world.migrator.provenance(world.domain, late_id),
            "migration_rows_remaining": world.migrator.status()["migration_rows_remaining"],
        },
        "unattributed_project_ids": sorted({pid for _, pid in after["s-unattributed"]}, key=str),
        "vectors_after": world.vectors(),
    }


def positive(root: Path) -> dict:
    first = migrate(root / "first")
    second = migrate(root / "second")
    return {"case": "A", **first, "independent_repeat_matches": first == second}


def rejection(root: Path) -> dict:
    world = World(root)
    world.migrator.startup()
    protected = world.protected()
    early = {
        "provenance_before_activation": refused(world.provenance),
        "activate_before_verify": refused(lambda: world.staged("activate")),
        "verify_before_backfill": refused(lambda: world.staged("verify")),
    }
    approvals = {
        "engineer_role": world.approval(role="engineer"),
        "no_actor": world.approval(actor=""),
        "other_migration": world.approval(migration_id=migrations.PROVENANCE),
    }
    denied = {name: refused(lambda a=a: world.migrator.dimensions.drop(a, 1.0)) for name, a in approvals.items()}
    restart = world.migrator.startup()
    unchanged = world.protected() == protected
    leaked = [
        text
        for text in [*early.values(), *denied.values()]
        if world.domain in text or "c-legacy" in text or world.doc["index"]["generation"] in text
    ]
    corrected = world.migrator.dimensions.drop(world.approval(), world.doc["clock"]["requeued"])
    return {
        "case": "B",
        "early_consumer_requests": early,
        "destructive_requests": denied,
        "startup_again": {"added": restart["added"], "blocked": restart["blocked"]},
        "protected_state_unchanged": unchanged,
        "diagnostics_name_no_scope": leaked == [],
        "operator_request": corrected,
        "vectors_after_operator_request": world.vectors(),
        "requeued_work": world.work(),
        "blocked_after_operator_request": world.migrator.dimensions.blocked(),
    }


def recovery(root: Path) -> dict:
    world = World(root)
    world.migrator.startup()
    identities = world.identities()
    stale_revision = world.migrator.status()["revision"]
    interrupted = ""
    try:
        world.backfill(Interrupting(world.catalog, world.doc["backfill"]["interrupt_after"]))
    except Interrupted as error:
        interrupted = str(error)
    during = stage(world.migrator.status())
    world.restart()
    stale = refused(lambda: world.migrator.backfill(stale_revision, 2, 1))
    resumed = stage(world.backfill())
    replayed = stage(world.backfill())
    provenance_rows = world.catalog.db.execute("SELECT COUNT(*) FROM session_event_provenance").fetchone()[0]
    world.staged("verify")
    world.staged("activate")
    world.staged("deactivate")
    rolled_back = {
        "provenance_read": refused(world.provenance),
        "state": world.migrator.status()["state"],
        "provenance_rows_retained": world.catalog.db.execute(
            "SELECT COUNT(*) FROM session_event_provenance"
        ).fetchone()[0],
        "previous_reader": world.previous_reader(),
    }
    reactivated = world.staged("activate")["state"]
    return {
        "case": "C",
        "interrupted": interrupted,
        "during_interruption": during,
        "stale_attempt": stale,
        "resumed": resumed,
        "replay_after_resume": replayed,
        "identities_unchanged": world.identities() == identities,
        "provenance_rows": provenance_rows,
        "rollback_rehearsal": rolled_back,
        "reactivated": reactivated,
    }


CASES = {"A": positive, "B": rejection, "C": recovery}


def run_case(case: str, root: Path) -> dict:
    return CASES[case](root)
