"""Revocation propagation: each consumer replays catalog tombstones after its durable cursor and converges.

Search projections, cached context packs and derived lessons of a session that is no longer retained are removed,
dropped or redacted. Pack and lesson reads also check the current session state, so a consumer that is behind on
tombstones still refuses revoked evidence.
"""

import json

from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import Catalog
from hooks.memory.index_worker import IndexWorker

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS revocation_cursor (
        access_domain TEXT NOT NULL, consumer TEXT NOT NULL, through INTEGER NOT NULL,
        PRIMARY KEY (access_domain, consumer))""",
    """CREATE TABLE IF NOT EXISTS revocation_applied (
        access_domain TEXT NOT NULL, consumer TEXT NOT NULL, sequence INTEGER NOT NULL, session_uid TEXT NOT NULL,
        lag_seconds REAL NOT NULL, refs TEXT NOT NULL, PRIMARY KEY (access_domain, consumer, sequence))""",
    """CREATE TABLE IF NOT EXISTS context_pack (
        access_domain TEXT NOT NULL, pack_id TEXT NOT NULL, body TEXT NOT NULL, sessions TEXT NOT NULL,
        PRIMARY KEY (access_domain, pack_id))""",
    """CREATE TABLE IF NOT EXISTS derived_lesson (
        access_domain TEXT NOT NULL, lesson_id TEXT NOT NULL, text TEXT, support TEXT NOT NULL, state TEXT NOT NULL,
        PRIMARY KEY (access_domain, lesson_id))""",
)
CONSUMERS = ("search", "packs", "lessons")
METRIC = "revocation_propagation_lag_seconds"
SESSION_KEY = "access_domain = ? AND session_uid = ?"
PROJECTIONS = ("search_chunk", "chunk_generation", "chunk_cutover", "chunk_vector", "embedding_work")
CHUNK_IDS = f"SELECT chunk_id FROM search_chunk WHERE {SESSION_KEY} ORDER BY chunk_id"
CITES = "EXISTS (SELECT 1 FROM json_each({column}) WHERE value = ?)"
PACKS_CITING = f"SELECT pack_id FROM context_pack WHERE access_domain = ? AND {CITES.format(column='sessions')}"
LESSONS_CITING = (
    f"SELECT lesson_id, support FROM derived_lesson WHERE access_domain = ? AND {CITES.format(column='support')}"
)
DROP_PACK = "DELETE FROM context_pack WHERE access_domain = ? AND pack_id = ?"
SET_LESSON = "UPDATE derived_lesson SET text = ?, support = ?, state = ? WHERE access_domain = ? AND lesson_id = ?"
PUT_PACK = "INSERT OR REPLACE INTO context_pack VALUES (?, ?, ?, ?)"
PUT_LESSON = "INSERT OR REPLACE INTO derived_lesson VALUES (?, ?, ?, ?, 'inferred')"
PACK = "SELECT body, sessions FROM context_pack WHERE access_domain = ? AND pack_id = ?"
LESSON = "SELECT text, support, state FROM derived_lesson WHERE access_domain = ? AND lesson_id = ?"
CURSOR = "SELECT through FROM revocation_cursor WHERE access_domain = ? AND consumer = ?"
ADVANCE = (
    "INSERT INTO revocation_cursor VALUES (?, ?, ?) "
    "ON CONFLICT (access_domain, consumer) DO UPDATE SET through = excluded.through"
)
APPLIED = "INSERT OR REPLACE INTO revocation_applied VALUES (?, ?, ?, ?, ?, ?)"
LAGS = (
    "SELECT consumer, sequence, session_uid, lag_seconds FROM revocation_applied WHERE access_domain = ? "
    "ORDER BY consumer, sequence"
)
EXPOSED = {
    **{table: f"SELECT COUNT(*) FROM {table} WHERE {SESSION_KEY}" for table in PROJECTIONS},
    "context_pack": f"SELECT COUNT(*) FROM context_pack WHERE access_domain = ? AND {CITES.format(column='sessions')}",
    "derived_lesson": (
        f"SELECT COUNT(*) FROM derived_lesson WHERE access_domain = ? AND {CITES.format(column='support')}"
    ),
}
WITHHELD = "the cached context is not available to this grant"
UNKNOWN_CONSUMER = "the consumer must be search, packs or lessons"
REVALIDATED, REDACTED = "revalidated", "redacted"


class Withheld(Exception):
    pass


class Propagator:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        IndexWorker(catalog)
        for statement in SCHEMA:
            catalog.db.execute(statement)
        self.consumers = {"search": self._search, "packs": self._packs, "lessons": self._lessons}

    def _retained(self, domain: str, session_uid: str) -> bool:
        found = self.catalog.session(domain, session_uid)
        return found is not None and found["state"] == "retained"

    def _search(self, domain: str, session_uid: str) -> list[str]:
        removed = [row[0] for row in self.catalog.db.execute(CHUNK_IDS, (domain, session_uid)).fetchall()]
        for table in PROJECTIONS:
            self.catalog.db.execute(f"DELETE FROM {table} WHERE {SESSION_KEY}", (domain, session_uid))
        return removed

    def _packs(self, domain: str, session_uid: str) -> list[str]:
        dropped = [row[0] for row in self.catalog.db.execute(PACKS_CITING, (domain, session_uid)).fetchall()]
        for pack_id in dropped:
            self.catalog.db.execute(DROP_PACK, (domain, pack_id))
        return sorted(dropped)

    def _lessons(self, domain: str, session_uid: str) -> list[str]:
        touched = []
        for lesson_id, support in self.catalog.db.execute(LESSONS_CITING, (domain, session_uid)).fetchall():
            permitted = [uid for uid in json.loads(support) if self._retained(domain, uid)]
            text = None if not permitted else self.catalog.db.execute(LESSON, (domain, lesson_id)).fetchone()[0]
            state = REVALIDATED if permitted else REDACTED
            self.catalog.db.execute(SET_LESSON, (text, json.dumps(permitted), state, domain, lesson_id))
            touched.append(lesson_id)
        return sorted(touched)

    def through(self, domain: str, consumer: str) -> int:
        row = self.catalog.db.execute(CURSOR, (domain, consumer)).fetchone()
        return row[0] if row else 0

    def _step(self, domain: str, consumer: str, tombstone: dict, now: float) -> dict | None:
        with self.catalog._transaction():
            if self.through(domain, consumer) != tombstone["sequence"] - 1:
                return None
            uid = tombstone["session_uid"]
            refs = [] if self._retained(domain, uid) else self.consumers[consumer](domain, uid)
            lag = round(now - tombstone["recorded_at"], 6)
            self.catalog.db.execute(APPLIED, (domain, consumer, tombstone["sequence"], uid, lag, json.dumps(refs)))
            self.catalog.db.execute(ADVANCE, (domain, consumer, tombstone["sequence"]))
        return {"consumer": consumer, "sequence": tombstone["sequence"], "session_uid": uid, "references": refs}

    def replay(self, domain: str, consumer: str, now: float) -> list[dict]:
        if consumer not in self.consumers:
            raise ValueError(UNKNOWN_CONSUMER)
        notices = []
        for tombstone in self.catalog.tombstones(domain, self.through(domain, consumer)):
            notice = self._step(domain, consumer, tombstone, now)
            if notice is not None:
                notices.append(notice)
        return notices

    def catch_up(self, domain: str, now: float) -> list[dict]:
        return [notice for consumer in CONSUMERS for notice in self.replay(domain, consumer, now)]

    def lags(self, domain: str) -> list[dict]:
        rows = self.catalog.db.execute(LAGS, (domain,)).fetchall()
        return [dict(zip(("consumer", "sequence", "session_uid", METRIC), row)) for row in rows]

    def exposures(self, domain: str, session_uid: str) -> dict[str, int]:
        counts = {
            name: self.catalog.db.execute(query, (domain, session_uid)).fetchone()[0] for name, query in EXPOSED.items()
        }
        return {name: count for name, count in counts.items() if count}

    def put_pack(self, domain: str, pack_id: str, body: dict, sessions: list[str]) -> None:
        self.catalog.db.execute(PUT_PACK, (domain, pack_id, json.dumps(body), json.dumps(sorted(set(sessions)))))

    def pack(self, grant: Grant, domain: str, pack_id: str) -> dict:
        row = self.catalog.db.execute(PACK, (domain, pack_id)).fetchone()
        if domain != grant.corpus or row is None:
            raise Withheld(WITHHELD)
        if not all(self._retained(domain, uid) for uid in json.loads(row[1])):
            raise Withheld(WITHHELD)
        return json.loads(row[0])

    def put_lesson(self, domain: str, lesson_id: str, text: str, support: list[str]) -> None:
        self.catalog.db.execute(PUT_LESSON, (domain, lesson_id, text, json.dumps(sorted(set(support)))))

    def lesson(self, grant: Grant, domain: str, lesson_id: str) -> dict:
        row = self.catalog.db.execute(LESSON, (domain, lesson_id)).fetchone()
        if domain != grant.corpus or row is None:
            raise Withheld(WITHHELD)
        permitted = [uid for uid in json.loads(row[1]) if self._retained(domain, uid)]
        if not permitted or row[0] is None:
            return {"lesson_id": lesson_id, "state": REDACTED, "text": None, "support": []}
        return {"lesson_id": lesson_id, "state": row[2], "text": row[0], "support": permitted}
