import hashlib
import json
from pathlib import Path
from unittest import mock

from hooks.memory import archive
from hooks.memory import catalog as catalog_module
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, ingest
from hooks.memory.catalog import Catalog, Conflict
from hooks.memory.chunking import ChunkIndex, ChunkScope, Policy
from hooks.memory.embeddings import EmbeddingModel
from hooks.memory.index_worker import IndexWorker, Target
from scripts.swarm_v2 import garbage_collection as gc

FIXTURE = Path(__file__).parent / "fixtures" / "sv2_ret05" / "fixture.json"
CASES_MODULE = Path(__file__)
FIXTURE_INPUTS = {
    "uncommitted_upload": "an upload whose reservation expired past grace without an archive entry, beside one whose bytes never landed",
    "active_index": "two embedding generations over the committed session, the newer one active and the older one retired",
    "foreign_lookalike_object": "a digest named object in a managed shard with no reservation, a stray root file and another domain's upload",
    "newly_referenced_checkpoint": "an abandoned upload that a task checkpoint references after collection claimed it",
}
EVIDENCE_CLASS = (
    "mocked: a SQLite catalog file and a local content addressed object folder stand in for the central database and "
    "the archive bucket; a deterministic hash embedder builds vectors, a fake clock drives expiry and grace, and a "
    "patched unlink stands in for a collector crash; no Pod, bucket or database runs"
)
VECTORS = "SELECT index_generation, COUNT(*) FROM chunk_vector GROUP BY index_generation ORDER BY index_generation"
RESERVATIONS = "SELECT object_digest, state FROM upload_reservation ORDER BY object_digest"


class Interrupted(OSError):
    pass


class FixtureEmbedder:
    def __init__(self, model):
        self.model = model

    def embed(self, texts):
        digests = [hashlib.sha256(text.encode()).digest() for text in texts]
        return [[round(digest[index] / 255, 6) for index in range(self.model.dimension)] for digest in digests]


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def grant(doc, corpus=None) -> Grant:
    data = doc["grant"]
    return Grant(data["principal"], corpus or data["corpus"], frozenset(data["projects"]))


def line(record) -> bytes:
    return json.dumps(record, ensure_ascii=False).encode() + b"\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class World:
    def __init__(self, root: Path, referenced: bool = True):
        self.doc, self.root = fixture(), root
        self.domain, self.clock = self.doc["grant"]["corpus"], self.doc["clock"]
        self.catalog_path = root / "catalog.sqlite3"
        self.catalog = Catalog(self.catalog_path)
        self.store = archive.ObjectStore(root / "objects")
        self.reservations = gc.Reservations(self.catalog)
        self.committed = self.session()
        self.uploads = {name: self.abandon(name, text) for name, text in self.doc["uploads"].items()}
        self.reserve("never-landed", digest(self.doc["never_landed"]), "attempt-dead", self.clock["expired"])
        self.lookalike = self.store.put(self.doc["lookalike"].encode())
        (self.store.root / "stray.tmp").write_text(self.doc["stray"])
        self.altered = digest(self.doc["altered"]["reserved"])
        self.reserve("altered", self.altered, "attempt-altered", self.clock["expired"])
        self.store._path(self.altered).parent.mkdir(parents=True, exist_ok=True)
        self.store._path(self.altered).write_text(self.doc["altered"]["written"])
        self.pinned = {self.uploads["checkpoint"]} if referenced else set()
        self.index()

    def reserve(self, upload_id, object_digest, attempt, expires_at, corpus=None):
        return self.reservations.reserve(grant(self.doc, corpus), upload_id, object_digest, attempt, expires_at)

    def abandon(self, name, text) -> str:
        corpus = self.doc["foreign_corpus"] if name == "foreign" else None
        expires = self.clock["live_until"] if name == "live" else self.clock["expired"]
        found = self.reserve(f"upload-{name}", digest(text), f"attempt-{name}", expires, corpus)["object_digest"]
        assert self.store.put(text.encode()) == found
        return found

    def session(self) -> list[str]:
        item, doc = self.doc["session"], self.doc
        uid, generation = item["session_uid"], item["source_generation"]
        source = (self.domain, uid, generation, doc["provider"], item["origin"], doc["retention"])
        self.catalog.register(catalog_module.SessionSource(*source))
        sequence, cursor, committed = 0, 0, []
        for batch, turn in enumerate(item["turns"]):
            events, data, start = [], b"", cursor
            for record in turn:
                sequence, encoded = sequence + 1, line(record)
                events.append({"sequence": sequence, "byte_start": cursor, "byte_end": cursor + len(encoded)})
                events[-1].update({"project_id": doc["grant"]["projects"][0], "record": record})
                data, cursor = data + encoded, cursor + len(encoded)
            object_digest = hashlib.sha256(data).hexdigest()
            self.reserve(f"session-{batch}", object_digest, "attempt-session", self.clock["expired"])
            assert self.store.put(data) == object_digest
            entry = catalog_module.ArchiveEntry(self.domain, uid, generation, start, cursor, object_digest)
            assert self.reservations.commit(grant(doc), f"session-{batch}", self.store, entry) is True
            body = {
                "schema_version": SCHEMA_VERSION,
                "batch_id": f"capture:{uid}:{batch}",
                "access_domain": self.domain,
            }
            body.update({"session_uid": uid, "source_generation": generation, "events": events})
            assert ingest(self.catalog, grant(doc), body).get("batch_id") == body["batch_id"]
            committed.append(object_digest)
        return committed

    def index(self) -> None:
        doc, policy = self.doc, Policy(**self.doc["policy"])
        model, uid = EmbeddingModel(**doc["model"]), doc["session"]["session_uid"]
        worker = IndexWorker(self.catalog)
        ChunkIndex(self.catalog).build(grant(doc), ChunkScope(self.domain, uid), policy)
        for revision, name in enumerate(("retired", "active")):
            generation = doc["generations"][name]
            worker.register(self.domain, generation, model)
            worker.enqueue(grant(doc), Target(self.domain, uid, policy.version, generation), self.clock["indexed"])
            assert (
                worker.drain(FixtureEmbedder(model), self.domain, generation, self.clock["indexed"])["state"]
                == "drained"
            )
            worker.activate(self.domain, generation, revision)

    def retire(self, name="retired", at="retired") -> float:
        return gc.retire(self.catalog, grant(self.doc), self.doc["generations"][name], self.clock[at])

    def plan(self, at="collect", destructive=True) -> gc.Plan:
        return gc.Plan(grant(self.doc), lambda: frozenset(self.pinned), self.doc["grace"], self.clock[at], destructive)

    def collect(self, **changes) -> dict:
        return gc.collect(self.catalog, self.store, self.plan(**changes))

    def present(self) -> list[str]:
        return sorted(
            path.relative_to(self.store.root).as_posix() for path in self.store.root.rglob("*") if path.is_file()
        )

    def protected(self) -> dict:
        rows = {table: sorted(self.catalog.db.execute(f"SELECT * FROM {table}").fetchall()) for table in gc.GC_TABLES}
        return {"catalog": self.catalog.snapshot(), "gc": rows, "objects": self.present(), "vectors": self.vectors()}

    def vectors(self) -> list:
        return [list(row) for row in self.catalog.db.execute(VECTORS)]

    def readable(self, names) -> bool:
        return all(self.store.get(name) for name in names)


def refused(call) -> str:
    try:
        call()
    except (Conflict, ValueError) as error:
        return str(error)
    raise AssertionError("the call was not refused")


def summary(report: dict) -> dict:
    return {key: report[key] for key in ("mode", "committed", gc.BYTES, gc.REJECTIONS, "reclaimed", "protected")}


def collected(root: Path) -> dict:
    world = World(root)
    world.retire()
    report = world.collect()
    names = {digest: name for name, digest in world.uploads.items()}
    return {
        "report": summary(report),
        "reclaimed_uploads": sorted(names.get(item, "never_landed") for item in report["reclaimed"]["uploads"]),
        "committed_readable": world.readable(world.committed),
        "checkpoint_readable": world.readable(world.pinned),
        "vectors_after": world.vectors(),
        "objects_after": len(world.present()),
        "rerun": summary(world.collect()),
    }


def positive(root: Path) -> dict:
    first, second = collected(root / "first"), collected(root / "second")
    return {"case": "A", "first": first, "repeat_matches": first == second}


def rejection(root: Path) -> dict:
    world = World(root, referenced=False)
    refusals = {
        "retire_active": refused(lambda: world.retire("active")),
        "bad_digest": refused(lambda: world.reserve("bad", "not-a-digest", "attempt-bad", 1.0)),
        "wrong_object": refused(
            lambda: world.reservations.commit(
                grant(world.doc),
                "upload-abandoned",
                world.store,
                catalog_module.ArchiveEntry(world.domain, "unknown", "unknown", 0, 1, world.lookalike),
            )
        ),
    }
    before = world.protected()
    early = world.collect(at="early")
    inventory = world.collect(destructive=False)
    untouched = world.protected() == before
    world.retire()
    report = world.collect()
    return {
        "case": "B",
        "refusals": refusals,
        "early": summary(early),
        "inventory": summary(inventory),
        "inventory_and_early_left_state_unchanged": untouched,
        "collect": summary(report),
        "lookalike_kept": world.readable([world.lookalike]),
        "stray_kept": "stray.tmp" in world.present(),
    }


def recovery(root: Path) -> dict:
    world = World(root, referenced=False)
    world.retire()
    target = world.uploads["checkpoint"]
    unlink = Path.unlink

    def crashing(path, missing_ok=False):
        if path.name == target:
            raise Interrupted("the collector stopped while deleting an object")
        return unlink(path, missing_ok=missing_ok)

    with mock.patch.object(Path, "unlink", crashing):
        try:
            world.collect()
            crashed = False
        except Interrupted:
            crashed = True
    claimed = [list(row) for row in world.catalog.db.execute(RESERVATIONS) if row[0] == target]
    reserve_during = refused(lambda: world.reserve("again", target, "attempt-again", world.clock["live_until"]))
    world.pinned.add(target)
    rerun = world.collect()
    replay = world.collect()
    return {
        "case": "C",
        "crashed": crashed,
        "claimed_before_rerun": claimed,
        "reserve_during_collection": reserve_during,
        "rerun": summary(rerun),
        "checkpoint_readable": world.readable([target]),
        "reservations_after": [list(row) for row in world.catalog.db.execute(RESERVATIONS)],
        "replay": summary(replay),
        "reserve_after": world.reserve("again", target, "attempt-again", world.clock["live_until"])["upload_id"],
    }


def run_case(case: str, root: Path) -> dict:
    return {"A": positive, "B": rejection, "C": recovery}[case](root)
