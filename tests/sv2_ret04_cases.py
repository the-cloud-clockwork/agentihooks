import dataclasses
import hashlib
import json
from pathlib import Path

from hooks.memory import archive
from hooks.memory import catalog as catalog_module
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, ingest
from hooks.memory.catalog import Catalog
from hooks.memory.chunking import Policy
from hooks.memory.embeddings import EmbeddingModel
from scripts.swarm_v2 import backup

FIXTURE = Path(__file__).parent / "fixtures" / "sv2_ret04" / "fixture.json"
CASES_MODULE = Path(__file__)
FIXTURE_INPUTS = {
    "catalog_backup": "a session catalog with two retained sessions, copied without its chunk and vector projections",
    "immutable_objects": "content addressed archive objects for every session batch and the fixture task checkpoint",
    "brain_backup": "a brain export copied beside the ledger and coordination snapshot, each under its own key id",
    "missing_source_object": "the second archive object of the lost session, deleted from the source store before capture",
}
EVIDENCE_CLASS = (
    "mocked: a SQLite catalog file, local content addressed object folders and fixture bytes stand in for the "
    "central database, the archive bucket, the backup bucket, the hosted ledger, the swarm Redis and the brain export; "
    "a deterministic hash embedder rebuilds vectors and a fake clock measures time; no Pod, bucket or database runs"
)
EVENTS = "SELECT * FROM session_event ORDER BY access_domain, event_id"
VECTORS = "SELECT COUNT(*) FROM chunk_vector"


class Interrupted(OSError):
    pass


class Interrupting(archive.ObjectStore):
    def __init__(self, root, gets):
        super().__init__(root)
        self.gets = gets

    def get(self, digest):
        if self.gets == 0:
            raise Interrupted("the restore process stopped while copying archive objects")
        self.gets -= 1
        return super().get(digest)


class FixtureEmbedder:
    def __init__(self, model):
        self.model = model

    def embed(self, texts):
        digests = [hashlib.sha256(text.encode()).digest() for text in texts]
        return [[round(digest[index] / 255, 6) for index in range(self.model.dimension)] for digest in digests]


class Clock:
    def __init__(self, start, step):
        self.now, self.step = start - step, step

    def __call__(self):
        self.now += self.step
        return self.now


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def grant(doc, corpus=None) -> Grant:
    data = doc["grant"]
    return Grant(data["principal"], corpus or data["corpus"], frozenset(data["projects"]))


def line(record) -> bytes:
    return json.dumps(record, ensure_ascii=False).encode() + b"\n"


def lost_session(doc) -> dict:
    return next(item for item in doc["sessions"] if "lost_batch" in item)


class World:
    def __init__(self, root: Path):
        self.doc, self.root = fixture(), root
        self.domain = self.doc["grant"]["corpus"]
        self.catalog_path = root / "source" / "catalog.sqlite3"
        self.objects = archive.ObjectStore(root / "source" / "objects")
        self.backup = archive.ObjectStore(root / "backup")
        self.registry = backup.Registry(root / "backup-registry.json")
        catalog = Catalog(self.catalog_path)
        found = [self.session(catalog, item) for item in self.doc["sessions"]]
        catalog.close()
        self.lost = [digest for digest in found if digest]
        for digest in self.lost:
            self.objects._path(digest).unlink()
        materials = self.doc["checkpoint"]["materials"]
        self.checkpoint = tuple(self.objects.put(item["content"].encode()) for item in materials)

    def session(self, catalog, item) -> str | None:
        uid, generation = item["session_uid"], item["source_generation"]
        source = (self.domain, uid, generation, self.doc["provider"], item["origin"], self.doc["retention"])
        catalog.register(catalog_module.SessionSource(*source))
        sequence, cursor, lost = 0, 0, None
        for batch, turn in enumerate(item["turns"]):
            events, data, start = [], b"", cursor
            for record in turn:
                sequence, encoded = sequence + 1, line(record)
                project = self.doc["grant"]["projects"][0]
                events.append({"sequence": sequence, "byte_start": cursor, "byte_end": cursor + len(encoded)})
                events[-1].update({"project_id": project, "record": record})
                data, cursor = data + encoded, cursor + len(encoded)
            entry = catalog_module.ArchiveEntry(self.domain, uid, generation, start, cursor, self.objects.put(data))
            archive.acknowledge(catalog, self.objects, entry)
            body = {"schema_version": SCHEMA_VERSION, "batch_id": f"capture:{uid}:{batch}"}
            body.update({"access_domain": self.domain, "session_uid": uid, "source_generation": generation})
            assert ingest(catalog, grant(self.doc), {**body, "events": events}).get("batch_id") == body["batch_id"]
            if batch == item.get("lost_batch"):
                lost = entry.object_digest
        return lost

    def heal(self) -> None:
        lost = lost_session(self.doc)
        data = b"".join(line(record) for record in lost["turns"][lost["lost_batch"]])
        assert self.objects.put(data) == self.lost[0]

    def sources(self) -> backup.Sources:
        files = {name: text.encode() for name, text in self.doc["files"].items()}
        checkpoints = {self.doc["checkpoint"]["task_id"]: self.checkpoint}
        return backup.Sources(
            self.domain, self.catalog_path, self.objects, files, checkpoints, self.doc["versions"], self.doc["key_ids"]
        )

    def capture(self, at="captured") -> str:
        set_id = backup.capture(self.sources(), self.backup, self.doc["clock"][at])
        self.registry.record(set_id, self.doc["clock"][at])
        return set_id

    def access(self, withhold=None, corpus=None, versions=None) -> backup.Access:
        keyring = frozenset(key for key in self.doc["key_ids"].values() if key != withhold)
        accepted = {name: frozenset({version}) for name, version in self.doc["versions"].items()}
        return backup.Access(grant(self.doc, corpus), keyring, {**accepted, **(versions or {})})

    def projections(self) -> backup.Projections:
        model = EmbeddingModel(**self.doc["model"])
        return backup.Projections(Policy(**self.doc["policy"]), self.doc["index_generation"], FixtureEmbedder(model))

    def query(self, catalog, store) -> dict:
        found = archive.evidence(catalog, store, grant(self.doc), self.domain, self.doc["query_session"])
        events = [event for source in found["sources"] for event in source["events"]]
        reference = {**{key: found[key] for key in ("access_domain", "session_uid")}, **events[0]}
        reference["source_generation"] = found["sources"][0]["source_generation"]
        first = archive.source_range(catalog, store, grant(self.doc), reference)
        return {"complete": found["complete"], "events": len(events), "first_record": json.loads(first)["text"]}

    def plan(self, name="restore", **changes) -> backup.Drill:
        clock = self.doc["clock"]
        plan = backup.Drill(
            self.root / name,
            self.access(),
            self.projections(),
            self.query,
            self.doc["checkpoint"]["task_id"],
            Clock(clock["drill_start"], clock["step"]),
        )
        return dataclasses.replace(plan, **changes)

    def protected(self) -> dict:
        catalog = Catalog(self.catalog_path)
        try:
            rows = catalog.snapshot()
        finally:
            catalog.close()
        folders = (self.objects.root, self.backup.root)
        stored = {
            path.name: path.read_bytes() for folder in folders for path in sorted(folder.rglob("*")) if path.is_file()
        }
        registry = self.registry.path.read_bytes() if self.registry.path.exists() else b""
        return {"catalog": rows, "objects": stored, "registry": registry}

    def restored_rows(self, name="restore") -> tuple[list, int]:
        catalog = Catalog(self.root / name / backup.CATALOG_FILE)
        try:
            return catalog.db.execute(EVENTS).fetchall(), catalog.db.execute(VECTORS).fetchone()[0]
        finally:
            catalog.close()

    def source_rows(self) -> list:
        catalog = Catalog(self.catalog_path)
        try:
            return catalog.db.execute(EVENTS).fetchall()
        finally:
            catalog.close()


def refused(call) -> str:
    try:
        call()
    except backup.Refused as error:
        return str(error)
    return "accepted"


def lost_query(world, name="restore") -> str:
    catalog = Catalog(world.root / name / backup.CATALOG_FILE)
    store = archive.ObjectStore(world.root / name / "objects")
    uid = lost_session(world.doc)["session_uid"]
    try:
        archive.evidence(catalog, store, grant(world.doc), world.domain, uid)
    except archive.EvidenceIntegrityError as error:
        return str(error)
    finally:
        catalog.close()
    return "returned"


def object_files(root: Path) -> int:
    return sum(path.is_file() for path in (root / "objects").rglob("*"))


def present(world, set_id) -> int:
    return sum(state == backup.PRESENT for state in json.loads(world.backup.get(set_id))["objects"].values())


def drilled(root: Path) -> dict:
    world = World(root)
    set_id = world.capture()
    before = world.protected()
    report = backup.drill(world.backup, set_id, world.plan("first-drill"))
    entry = world.registry.drill(world.backup, set_id, 1, world.plan())
    rows, vectors = world.restored_rows()
    files = {name: (world.root / "restore" / name).read_bytes().decode() for name in backup.FILES}
    after = world.protected()
    return {
        "report": {key: value for key, value in report.items() if key != "set_id"},
        "report_names_its_set": report["set_id"] == set_id,
        "registry_status": entry["status"],
        "registry_matches_report": entry["report"] == hashlib.sha256(backup._encoded(report)).hexdigest(),
        "events_match_source": rows == world.source_rows(),
        "files_match_source": files == world.doc["files"],
        "vectors_rebuilt": vectors,
        "source_unchanged": (before["catalog"], before["objects"]) == (after["catalog"], after["objects"]),
        "lost_session_query": lost_query(world),
        "accepted": world.registry.accepted(set_id),
        "retention_allowed": world.registry.retention_allowed(),
        "preserved_is_this_set": world.registry.preserved() == set_id,
        "metric": world.registry.status(),
        "lost_objects": len(world.lost),
    }


def positive(root: Path) -> dict:
    first, second = drilled(root / "first"), drilled(root / "second")
    healed = World(root / "healed")
    healed.heal()
    healed_set = healed.capture()
    entry = healed.registry.drill(healed.backup, healed_set, 1, healed.plan())
    full = {"status": entry["status"], "retention_allowed": healed.registry.retention_allowed()}
    return {"case": "A", **first, "repeat_matches": first == second, "without_missing_object": full}


def refusals(world, set_id, occupied) -> dict:
    manifest = json.loads(world.backup.get(set_id))
    future = world.backup.put(json.dumps({**manifest, "format": "2.0"}, sort_keys=True).encode())
    retired = world.doc["retired_version"]
    retired_access = world.access(versions={retired["component"]: frozenset({retired["accepted"]})})
    calls = {
        "missing_key": (set_id, "nokey", world.access(withhold=world.doc["withheld_key"])),
        "foreign_corpus": (set_id, "foreign", world.access(corpus=world.doc["foreign_corpus"])),
        "unsupported_version": (set_id, "retired", retired_access),
        "unsupported_format": (future, "future", world.access()),
    }
    found = {
        name: refused(lambda call=call: backup.restore(world.backup, call[0], world.root / call[1], call[2]))
        for name, call in calls.items()
    }
    found["not_isolated"] = refused(lambda: backup.restore(world.backup, set_id, occupied, world.access()))
    found["unknown_set"] = refused(lambda: world.registry.drill(world.backup, "0" * 64, 1, world.plan("unknown")))
    created = {name: (world.root / call[1]).exists() for name, call in calls.items()}
    created["unknown_set"] = (world.root / "unknown").exists()
    return {"messages": found, "targets_created": created}


def rejection(root: Path) -> dict:
    world = World(root)
    set_id = world.capture()
    upload_only = {
        "accepted": world.registry.accepted(set_id),
        "retention_allowed": world.registry.retention_allowed(),
        "preserved": world.registry.preserved(),
        "metric": world.registry.status(),
    }
    occupied = world.root / "occupied"
    occupied.mkdir()
    (occupied / "other.txt").write_text("another restore's data")
    before = world.protected()
    refusal = refusals(world, set_id, occupied)
    after = world.protected()
    unchanged = (before["catalog"], before["registry"]) == (after["catalog"], after["registry"]) and all(
        after["objects"].get(name) == data for name, data in before["objects"].items()
    )
    fragments = [*world.doc["key_ids"].values(), str(world.root), set_id, world.doc["foreign_corpus"]]
    disclosed = sorted(
        {fragment for message in refusal["messages"].values() for fragment in fragments if fragment in message}
    )
    tampered = World(root / "tampered")
    tampered_set = tampered.capture()
    ledger = json.loads(tampered.backup.get(tampered_set))["components"]["ledger"]["digest"]
    tampered.backup._path(ledger).write_bytes(b"tampered ledger")
    failed = tampered.registry.drill(tampered.backup, tampered_set, 1, tampered.plan())
    corrected = world.registry.drill(world.backup, set_id, 1, world.plan("corrected"))
    return {
        "case": "B",
        "upload_without_drill": upload_only,
        "refusals": refusal["messages"],
        "refused_targets_created": refusal["targets_created"],
        "occupied_target": sorted(path.name for path in occupied.iterdir()),
        "protected_state_unchanged": unchanged,
        "disclosed_fragments": disclosed,
        "tampered_drill_status": failed["status"],
        "tampered_accepted": tampered.registry.accepted(tampered_set),
        "corrected_request": corrected["status"],
    }


def rehearsal(world, set_id) -> dict:
    newer = world.capture("later_capture")
    newer_set = {
        "retention_allowed": world.registry.retention_allowed(),
        "preserved_is_drilled_set": world.registry.preserved() == set_id,
        "newer_set_accepted": world.registry.accepted(newer),
    }
    brain = json.loads(world.backup.get(set_id))["components"]["brain"]["digest"]
    world.backup._path(brain).write_bytes(b"damaged brain export")
    demoted = world.registry.drill(world.backup, set_id, 3, world.plan("redrill"))
    return {
        "newer_unverified_set": newer_set,
        "redrill_after_damage": demoted["status"],
        "damaged_set_accepted": world.registry.accepted(set_id),
        "preserved_after_failed_redrill": world.registry.preserved() == set_id,
        "metric": world.registry.status(),
    }


def recovery(root: Path) -> dict:
    world = World(root)
    set_id = world.capture()
    target = world.root / "restore"
    interrupted = ""
    try:
        backup.restore(
            Interrupting(world.backup.root, world.doc["interrupt_after_gets"]), set_id, target, world.access()
        )
    except Interrupted as error:
        interrupted = str(error)
    during = {
        "objects": object_files(target),
        "catalog_written": (target / backup.CATALOG_FILE).exists(),
        "marker_names_set": json.loads((target / backup.MARKER).read_text())["set_id"] == set_id,
    }
    accepted = world.registry.drill(world.backup, set_id, 2, world.plan())
    _, vectors = world.restored_rows()
    replay = world.registry.drill(world.backup, set_id, 2, world.plan())
    _, replayed_vectors = world.restored_rows()
    stale = refused(lambda: world.registry.drill(world.backup, set_id, 1, world.plan("stale")))
    manifest = json.loads(world.backup.get(set_id))
    return {
        "case": "C",
        "interrupted": interrupted,
        "during_interruption": during,
        "resumed_status": accepted["status"],
        "registry_replay_unchanged": replay == accepted,
        "objects_after_resume": object_files(target),
        "present_in_backup": present(world, set_id),
        "vectors_rebuilt": vectors,
        "vectors_after_replay": replayed_vectors,
        "omitted_projections": manifest["omitted_projections"],
        "irrecoverable": {digest: state for digest, state in manifest["objects"].items() if state != backup.PRESENT},
        "lost_session_query": lost_query(world),
        "stale_attempt": stale,
        "stale_target_created": (world.root / "stale").exists(),
        "rollback_rehearsal": rehearsal(world, set_id),
    }


CASES = {"A": positive, "B": rejection, "C": recovery}


def run_case(case: str, root: Path) -> dict:
    return CASES[case](root)
