import json
from pathlib import Path

from hooks.memory import archive, revocation
from hooks.memory.api import records
from tests.sv2_idx04_cases import Corpus, grant, request

FIXTURE = Path(__file__).parent / "fixtures" / "sv2_ret02" / "fixture.json"
FIXTURE_INPUTS = {
    "source_in_search_results": "session lock, the top hit of the recall search and the cited source of its records",
    "cached_pack": "pack-ingest-lock, previews of the recall search results citing lock, beside pack-kernel-feed",
    "inferred_lesson": "lesson-lock-only supported by lock alone, beside lesson-shared supported by lock, kernel and symbol",
}


class Interrupted(OSError):
    pass


class Flaky(revocation.Propagator):
    def __init__(self, catalog):
        super().__init__(catalog)
        self.consumers["lessons"] = self._failing

    def _failing(self, domain, session_uid):
        self._lessons(domain, session_uid)
        raise Interrupted("the lessons consumer stopped before its cursor moved")


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class World:
    def __init__(self, root: Path):
        self.doc = fixture()
        self.inputs = json.loads((FIXTURE.parent.parent / self.doc["base"]).read_text(encoding="utf-8"))
        self.domain, self.root = self.inputs["grant"]["corpus"], root
        self.corpus = Corpus(self.inputs, root)
        self.corpus.index()
        self.open()
        self.seed()

    def open(self):
        self.tools = records.SessionTools(self.corpus.catalog, archive.ObjectStore(self.root / "central/objects"))
        self.propagator = revocation.Propagator(self.corpus.catalog)

    def restart(self):
        self.corpus.restart()
        self.open()

    def uid(self, name: str) -> str:
        return next(item["session_uid"] for item in self.inputs["sessions"] if item["name"] == name)

    def short(self, uid: str) -> str:
        return next(item["name"] for item in self.inputs["sessions"] if item["session_uid"] == uid)

    def ask(self, name: str, **fields) -> dict:
        body = {key: value for key, value in request(self.inputs, "").items() if key != "query"}
        return records.call(self.tools, name, grant(self.inputs), {**body, **fields}, self.doc["clock"]["revoked"])

    def search(self, name="session_search") -> dict:
        return self.ask(name, query=self.inputs["queries"][self.doc["packs"]["cited"]["query"]])

    def seed(self):
        results = self.search()["results"]
        self.citation = next(item["citation"] for item in results if self.short(item["session_uid"]) == "lock")
        cited, kept = self.doc["packs"]["cited"], self.doc["packs"]["kept"]
        previews = [{"chunk_id": item["chunk_id"], "preview": item["preview"]} for item in results]
        self.propagator.put_pack(
            self.domain, cited["pack_id"], {"previews": previews}, [r["session_uid"] for r in results]
        )
        chunks = self.corpus.sessions[kept["session"]].chunks("current")
        body = {"previews": [{"chunk_id": chunk["chunk_id"], "preview": chunk["preview"]} for chunk in chunks]}
        self.propagator.put_pack(self.domain, kept["pack_id"], body, [self.uid(kept["session"])])
        for lesson in self.doc["lessons"]:
            support = [self.uid(name) for name in lesson["support"]]
            self.propagator.put_lesson(self.domain, lesson["lesson_id"], lesson["text"], support)

    def revoke(self, name: str, at: float, state="deleted") -> int:
        found = self.corpus.catalog.session(self.domain, self.uid(name))
        return self.corpus.catalog.set_state(self.domain, self.uid(name), state, found["revision"], at)

    def pack(self, key: str) -> str:
        try:
            self.propagator.pack(grant(self.inputs), self.domain, self.doc["packs"][key]["pack_id"])
        except revocation.Withheld as error:
            return str(error)
        return "readable"

    def lessons(self) -> dict:
        shown = {}
        for lesson in self.doc["lessons"]:
            found = self.propagator.lesson(grant(self.inputs), self.domain, lesson["lesson_id"])
            shown[lesson["lesson_id"]] = {**found, "support": [self.short(uid) for uid in found["support"]]}
        return shown

    def surfaces(self) -> dict:
        names = ("session_records", "get_session_segment")
        reads = {name: refused(self.ask(name, citation=self.citation)) for name in names}
        hits = {
            name: sorted({self.short(r["session_uid"]) for r in self.search(name)["results"]})
            for name in ("session_search", "search_sessions")
        }
        return {
            "searched_sessions": hits,
            "range_reads": reads,
            "packs": {key: self.pack(key) for key in self.doc["packs"]},
            "lessons": self.lessons(),
            "exposures": self.propagator.exposures(self.domain, self.uid(self.doc["source"])),
        }

    def held_events(self, name: str) -> int:
        uid = self.uid(name)
        return sum(
            len(self.corpus.catalog.events(self.domain, uid, s["source_generation"]))
            for s in self.corpus.catalog.sources(self.domain, uid)
        )

    def notices(self, notices: list[dict]) -> list[dict]:
        return [
            {**item, "session_uid": self.short(item["session_uid"]), "references": len(item["references"])}
            if item["consumer"] == "search"
            else {**item, "session_uid": self.short(item["session_uid"])}
            for item in notices
        ]

    def lags(self) -> list[dict]:
        return [{**item, "session_uid": self.short(item["session_uid"])} for item in self.propagator.lags(self.domain)]


def refused(answer: dict) -> str:
    return answer["error"]["class"] if answer["state"] == "rejected" else "answered"


def positive(root: Path) -> dict:
    runs = []
    for index in (1, 2):
        world = World(root / f"fixture-{index}")
        clock = world.doc["clock"]
        before = world.surfaces()
        tombstone = world.revoke("lock", clock["revoked"])
        notices = world.propagator.catch_up(world.domain, clock["propagated"])
        runs.append(
            {
                "before": before,
                "revision": tombstone,
                "tombstones": [
                    {**item, "session_uid": world.short(item["session_uid"])}
                    for item in world.corpus.catalog.tombstones(world.domain)
                ],
                "notices": world.notices(notices),
                "after": world.surfaces(),
                "authoritative_events_kept": world.held_events("lock"),
                "lag": world.lags(),
                "replayed": world.propagator.catch_up(world.domain, clock["restarted"]),
            }
        )
    return {"case": "A", "first": runs[0], "independent_fixture_matches": runs[0] == runs[1]}


def rejection(root: Path) -> dict:
    world = World(root)
    clock, uid = world.doc["clock"], world.uid("lock")
    world.revoke("lock", clock["revoked"])
    world.corpus.catalog.db.execute(
        "DELETE FROM chunk_vector WHERE access_domain = ? AND session_uid = ?", (world.domain, uid)
    )
    vector_only = world.propagator.exposures(world.domain, uid)
    protected = world.corpus.protected()
    surfaces = world.surfaces()
    diagnostics = json.dumps(surfaces)
    foreign = grant(world.inputs, corpus=world.inputs["foreign_corpus"])
    try:
        world.propagator.pack(foreign, world.domain, world.doc["packs"]["kept"]["pack_id"])
        foreign_pack = "readable"
    except revocation.Withheld as error:
        foreign_pack = str(error)
    try:
        world.propagator.replay(world.domain, "vectors", clock["propagated"])
        unknown = "accepted"
    except ValueError as error:
        unknown = str(error)
    unchanged = world.corpus.protected() == protected
    world.propagator.catch_up(world.domain, clock["propagated"])
    return {
        "case": "B",
        "vector_only_deletion": {"exposures": vector_only, "contract_failed": bool(vector_only)},
        "reads_during_vector_only_state": surfaces,
        "state_unchanged_by_refused_reads": unchanged,
        "diagnostics_name_no_source": uid not in diagnostics and "database is locked" not in diagnostics,
        "foreign_grant_pack": foreign_pack,
        "unknown_consumer": unknown,
        "after_full_propagation": world.propagator.exposures(world.domain, uid),
    }


def recovery(root: Path) -> dict:
    world = World(root)
    clock = world.doc["clock"]
    world.revoke("lock", clock["revoked"])
    for consumer in ("search", "packs"):
        world.propagator.replay(world.domain, consumer, clock["propagated"])
    world.revoke("symbol", clock["downtime_revoked"])
    interrupted = ""
    try:
        Flaky(world.corpus.catalog).replay(world.domain, "lessons", clock["downtime_revoked"])
    except Interrupted as error:
        interrupted = str(error)
    during = {
        "cursors": {name: world.propagator.through(world.domain, name) for name in revocation.CONSUMERS},
        "lessons": world.lessons(),
    }
    world.restart()
    resumed = world.propagator.catch_up(world.domain, clock["restarted"])
    replay = world.propagator.catch_up(world.domain, clock["restarted"])
    first = world.corpus.catalog.tombstones(world.domain)[0]
    stale = world.propagator._step(world.domain, "lessons", first, clock["restarted"])
    converged = {name: world.propagator.exposures(world.domain, world.uid(name)) for name in ("lock", "symbol")}
    lags = world.lags()
    world.revoke("lock", clock["regranted"], "retained")
    regranted = world.surfaces()
    world.corpus.catalog.db.execute("DELETE FROM revocation_cursor WHERE access_domain = ?", (world.domain,))
    rebuilt = world.propagator.catch_up(world.domain, clock["regranted"])
    return {
        "case": "C",
        "interrupted": interrupted,
        "during_downtime": during,
        "resumed": world.notices(resumed),
        "replay_after_resume": replay,
        "stale_attempt": stale,
        "converged_exposures": converged,
        "lag": lags,
        "after_regrant": {
            "lessons": regranted["lessons"],
            "packs": regranted["packs"],
            "exposures": regranted["exposures"],
        },
        "rollback_rehearsal": {
            "rewound_notices": world.notices(rebuilt),
            "lessons": world.lessons(),
            "packs": {key: world.pack(key) for key in world.doc["packs"]},
        },
    }


CASES = {"A": positive, "B": rejection, "C": recovery}


def run_case(case: str, root: Path) -> dict:
    return CASES[case](root)
