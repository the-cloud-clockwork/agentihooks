import json
import re
import sqlite3
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from .store import SQLiteRecallStore

DAY = 86_400_000
SPANS = {"m": 60_000, "h": 3_600_000, "d": DAY, "w": 7 * DAY}
SPAN = re.compile(r"(\d+)\s*(m|mins?|minutes?|h|hours?|d|days?|w|weeks?)")
WORD = re.compile(r"[\w-]+")
REF = re.compile(r"\b[a-z]+/[\w./@-]+")
PR_NUMBER = re.compile(r"(?:#|\bpr\s*#?|\bpull request\s*#?)(\d+)\b", re.IGNORECASE)
ID_COLLECTIONS = ("tasks", "phases", "followups", "questions")
COLUMNS = (
    "records.key, records.source, records.ledger_slug, records.swarm_slug, records.kind, records.ref,"
    " records.parent_ref, records.author, records.time, records.title, records.text, records.source_state"
)


@dataclass(frozen=True)
class RankWeights:
    title: float = 4.0
    body: float = 1.0
    relevance: float = 1.0
    recency: float = 0.3
    half_life_days: float = 7.0
    candidates: int = 200
    kinds: dict[str, float] = field(
        default_factory=lambda: {
            "task": 0.2,
            "handoff": 0.15,
            "learned": 0.15,
            "recap": 0.1,
            "phase": 0.1,
            "culture": 0.05,
            "followup": 0.05,
            "question": 0.05,
        }
    )


WEIGHTS = RankWeights()


@dataclass(frozen=True)
class Filters:
    scope: str = "all"
    kinds: tuple[str, ...] = ()
    author: str = ""
    seat: str = ""
    since: str | int | None = None
    until: str | int | None = None


@dataclass(frozen=True)
class Parent:
    ref: str
    kind: str
    title: str


@dataclass(frozen=True)
class Hit:
    item: str
    ledger_slug: str
    swarm_slug: str
    kind: str
    ref: str
    title: str
    author: str
    time: int
    archived: bool
    exact: bool
    score: float
    snippet: str
    parents: tuple[Parent, ...] = ()


@dataclass(frozen=True)
class Entry:
    item: str
    ledger_slug: str
    swarm_slug: str
    kind: str
    ref: str
    title: str
    author: str
    time: int


@dataclass(frozen=True)
class _Row:
    key: str
    source: str
    ledger_slug: str
    swarm_slug: str
    kind: str
    ref: str
    parent_ref: str
    author: str
    time: int
    title: str
    text: str
    source_state: str

    @property
    def item(self) -> str:
        return self.key.rsplit("#", 1)[0]


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_time(value: str | int | None, now: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = value.strip()
    if match := SPAN.fullmatch(text):
        return now - int(match[1]) * SPANS[match[2][0]]
    if text.isdigit():
        return int(text)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"not an ISO time, epoch milliseconds or span such as 1d: {value}") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def _where(filters: Filters, now: int) -> tuple[str, list]:
    clauses, params = [], []
    if filters.scope not in ("", "all"):
        clauses.append("(records.ledger_slug = ? OR records.swarm_slug = ?)")
        params += [filters.scope, filters.scope]
    if filters.kinds:
        clauses.append(f"records.kind IN ({', '.join('?' for _ in filters.kinds)})")
        params += list(filters.kinds)
    if filters.author:
        clauses.append("records.author = ?")
        params.append(filters.author)
    if filters.seat:
        prefix = f"seats/{filters.seat}/"
        clauses.append("(substr(records.ref, 1, ?) = ? OR instr(records.text, ?) > 0)")
        params += [len(prefix), prefix, json.dumps({"seat": filters.seat})[1:-1]]
    for bound, operator in ((filters.since, ">="), (filters.until, "<=")):
        moment = parse_time(bound, now)
        if moment is not None:
            clauses.append(f"records.time {operator} ?")
            params.append(moment)
    return " AND ".join(clauses) or "1", params


def _exact_refs(query: str) -> list[str]:
    found = REF.findall(query)
    found += [f"{collection}/{word}" for word in WORD.findall(query) for collection in ID_COLLECTIONS]
    return list(dict.fromkeys(found))


def _exact(connection: sqlite3.Connection, query: str, where: str, params: list) -> list[_Row]:
    wanted = _exact_refs(query)
    rows = []
    if wanted:
        found = connection.execute(
            f"SELECT {COLUMNS} FROM records WHERE {where} AND records.ref IN ({', '.join('?' for _ in wanted)})"
            " ORDER BY records.chunk_index",
            [*params, *wanted],
        ).fetchall()
        rows = sorted((_Row(*row) for row in found), key=lambda row: wanted.index(row.ref))
    for number in dict.fromkeys(PR_NUMBER.findall(query)):
        link = re.compile(rf"/pull/{number}(?!\d)")
        found = connection.execute(
            f"SELECT {COLUMNS} FROM records WHERE {where} AND records.kind = 'task' AND instr(records.text, ?) > 0"
            " ORDER BY records.time DESC, records.key",
            [*params, f"/pull/{number}"],
        ).fetchall()
        rows += [row for row in (_Row(*values) for values in found) if link.search(row.text)]
    return rows


def _ranked(connection: sqlite3.Connection, query: str, where: str, params: list, weights: RankWeights) -> list:
    words = WORD.findall(query)
    if not words:
        return []
    expression = " OR ".join(f'"{word}"' for word in words)
    return connection.execute(
        f"SELECT {COLUMNS}, bm25(recall_fts, ?, ?), snippet(recall_fts, -1, '[', ']', '…', 16)"
        f" FROM recall_fts JOIN records ON records.id = recall_fts.rowid"
        f" WHERE recall_fts MATCH ? AND {where} ORDER BY bm25(recall_fts, ?, ?) LIMIT ?",
        [weights.title, weights.body, expression, *params, weights.title, weights.body, weights.candidates],
    ).fetchall()


def _score(rank: float, best: float, row: _Row, weights: RankWeights, now: int) -> float:
    relevance = rank / best if best < 0 else 0.0
    recency = 0.5 ** (max(now - row.time, 0) / DAY / weights.half_life_days)
    return weights.relevance * relevance + weights.recency * recency + weights.kinds.get(row.kind, 0.0)


def _parents(connection: sqlite3.Connection, row: _Row) -> tuple[Parent, ...]:
    chain, ref = [], row.parent_ref
    while ref and ref not in {parent.ref for parent in chain}:
        found = connection.execute(
            "SELECT kind, title, parent_ref FROM records WHERE source = ? AND ref = ? ORDER BY chunk_index LIMIT 1",
            (row.source, ref),
        ).fetchone()
        if found is None:
            chain.append(Parent(ref, "", ""))
            break
        chain.append(Parent(ref, found[0], found[1]))
        ref = found[2]
    return tuple(chain)


def _hit(row: _Row, exact: bool, score: float, snippet: str) -> Hit:
    return Hit(
        item=row.item,
        ledger_slug=row.ledger_slug,
        swarm_slug=row.swarm_slug,
        kind=row.kind,
        ref=row.ref,
        title=row.title,
        author=row.author,
        time=row.time,
        archived=row.source_state == "archived",
        exact=exact,
        score=score,
        snippet=snippet,
    )


def search(
    store: SQLiteRecallStore,
    query: str,
    filters: Filters = Filters(),
    limit: int = 10,
    *,
    weights: RankWeights = WEIGHTS,
    now: int | None = None,
) -> list[Hit]:
    now = now_ms() if now is None else now
    where, params = _where(filters, now)
    with store.connect() as connection:
        chosen: dict[str, tuple[_Row, Hit]] = {}
        for row in _exact(connection, query, where, params):
            if row.item not in chosen:
                chosen[row.item] = (row, _hit(row, True, _score(0.0, 0.0, row, weights, now), row.text[:200]))
        ranked = _ranked(connection, query, where, params, weights)
        best = min((found[-2] for found in ranked), default=0.0)
        scored: dict[str, tuple[_Row, Hit]] = {}
        for found in ranked:
            row = _Row(*found[:-2])
            hit = _hit(row, False, _score(found[-2], best, row, weights, now), found[-1])
            if row.item not in chosen and (row.item not in scored or hit.score > scored[row.item][1].score):
                scored[row.item] = (row, hit)
        order = list(chosen.values()) + sorted(scored.values(), key=lambda pair: (-pair[1].score, pair[0].key))
        return [replace(hit, parents=_parents(connection, row)) for row, hit in order[:limit]]


def timeline(store: SQLiteRecallStore, filters: Filters = Filters(), limit: int = 200, *, now: int | None = None):
    where, params = _where(filters, now_ms() if now is None else now)
    with store.connect() as connection:
        found = connection.execute(
            f"SELECT {COLUMNS} FROM records WHERE {where} AND records.chunk_index = 0"
            " ORDER BY records.time, records.ledger_slug, records.ref, records.key LIMIT ?",
            [*params, limit],
        ).fetchall()
    return [
        Entry(row.item, row.ledger_slug, row.swarm_slug, row.kind, row.ref, row.title, row.author, row.time)
        for row in (_Row(*values) for values in found)
    ]
