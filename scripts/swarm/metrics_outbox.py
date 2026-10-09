import json
import math
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

URL_ENV = "AGENTIHOOKS_METRICS_URL"
USER_ENV = "AGENTIHOOKS_METRICS_USER"
PASSWORD_ENV = "AGENTIHOOKS_METRICS_PASSWORD"
DATABASE = "swarm"
DAY_MS = 24 * 60 * 60 * 1000
BATCH = 1000
TIMEOUT_S = 3
NAME = re.compile(r"[a-z][a-z0-9_]*")
BASE = (
    ("event_id", "String"),
    ("ledger", "String"),
    ("ts_ms", "Int64"),
    ("plan", "String"),
    ("phase", "String"),
    ("slice", "String"),
    ("task", "String"),
)
BASE_NAMES = tuple(name for name, _ in BASE)
RESERVED = (*BASE_NAMES, "ts")
REQUIRED = ("event_id", "ledger")
TIME_COLUMN = "ts DateTime64(3, 'UTC') DEFAULT fromUnixTimestamp64Milli(ts_ms, 'UTC')"
SPOOL = (
    "CREATE TABLE IF NOT EXISTS spool (tbl TEXT NOT NULL, event_id TEXT NOT NULL, ts_ms INTEGER NOT NULL,"
    " row TEXT NOT NULL, shipped INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (tbl, event_id))",
    "CREATE INDEX IF NOT EXISTS spool_pending ON spool (shipped, tbl)",
    "CREATE TABLE IF NOT EXISTS tables (name TEXT PRIMARY KEY, ddl TEXT NOT NULL)",
)
APPEND = "INSERT OR IGNORE INTO spool (tbl, event_id, ts_ms, row) VALUES (?, ?, ?, ?)"
REMEMBER = "INSERT OR REPLACE INTO tables (name, ddl) VALUES (?, ?)"
WAITING = "SELECT DISTINCT spool.tbl, tables.ddl FROM spool JOIN tables ON tables.name = spool.tbl WHERE shipped = 0"
BATCH_ROWS = "SELECT event_id, row FROM spool WHERE tbl = ? AND shipped = 0 ORDER BY ts_ms, event_id LIMIT ?"
SHIPPED = "UPDATE spool SET shipped = 1 WHERE tbl = ? AND event_id = ?"
PRUNE = "DELETE FROM spool WHERE shipped = 1 AND ts_ms < ?"
RECENT = "SELECT row FROM spool WHERE tbl = ? AND ts_ms >= ? ORDER BY ts_ms, event_id"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_float(value):
    return (_is_int(value) or isinstance(value, float)) and math.isfinite(value)


KINDS = {"String": lambda value: isinstance(value, str), "Int64": _is_int, "Float64": _is_float}


@dataclass(frozen=True)
class Settings:
    url: str
    user: str
    password: str


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple = ()

    def __post_init__(self):
        names = [column for column, _ in self.columns]
        if not NAME.fullmatch(self.name):
            raise ValueError(f"metrics table name {self.name!r} is not a plain lower case name")
        for column, kind in self.columns:
            if not NAME.fullmatch(column) or column in RESERVED or kind not in KINDS:
                raise ValueError(f"metrics table {self.name} has an unsafe column {column!r} of type {kind!r}")
        if len(set(names)) != len(names):
            raise ValueError(f"metrics table {self.name} names a column twice")

    def ddl(self):
        columns = ", ".join(f"{column} {kind}" for column, kind in (*BASE, *self.columns))
        return (
            f"CREATE TABLE IF NOT EXISTS {DATABASE}.{self.name} ({columns}, {TIME_COLUMN})"
            " ENGINE = ReplacingMergeTree ORDER BY (ledger, event_id)"
        )

    def check(self, row):
        schema = dict((*BASE, *self.columns))
        if not isinstance(row, dict) or set(row) != set(schema):
            raise ValueError(f"a {self.name} row must carry exactly the columns {sorted(schema)}")
        for column, kind in schema.items():
            if not KINDS[kind](row[column]):
                raise ValueError(f"{self.name}.{column} must be {kind}, got {row[column]!r}")
        if not all(row[column] for column in REQUIRED) or row["ts_ms"] <= 0:
            raise ValueError(f"a {self.name} row needs an event id, a ledger and a positive time")


def settings(environ):
    url = environ.get(URL_ENV, "").rstrip("/")
    if not url:
        return None
    return Settings(url, environ.get(USER_ENV) or "default", environ.get(PASSWORD_ENV, ""))


def spool_path():
    return Path.home() / ".agentihooks" / "swarm" / "metrics-outbox.sqlite"


def post(sink, query, body):
    request = urllib.request.Request(
        f"{sink.url}/?{urllib.parse.urlencode({'query': query})}",
        data=body,
        method="POST",
        headers={"X-ClickHouse-User": sink.user, "X-ClickHouse-Key": sink.password},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:
            return reply.status == 200
    except (urllib.error.URLError, OSError):
        return False


class Outbox:
    def __init__(self, path, sink, send=None):
        self.path = Path(path)
        self.sink = sink
        self.send = send or post
        self.created = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            for statement in SPOOL:
                self.db.execute(statement)

    def append(self, table, rows):
        for row in rows:
            table.check(row)
        with self.db:
            self.db.execute(REMEMBER, (table.name, table.ddl()))
            self.db.executemany(APPEND, [(table.name, row["event_id"], row["ts_ms"], json.dumps(row)) for row in rows])

    def flush(self, now_ms):
        shipped = 0
        for name, ddl in self.db.execute(WAITING).fetchall():
            sent, reached = self._ship(name, ddl)
            shipped += sent
            if not reached:
                break
        with self.db:
            self.db.execute(PRUNE, (now_ms - DAY_MS,))
        return shipped

    def _ship(self, name, ddl):
        if name not in self.created:
            if not self.send(self.sink, ddl, b""):
                return 0, False
            self.created.add(name)
        shipped = 0
        while batch := self.db.execute(BATCH_ROWS, (name, BATCH)).fetchall():
            body = "\n".join(row for _, row in batch).encode()
            if not self.send(self.sink, f"INSERT INTO {DATABASE}.{name} FORMAT JSONEachRow", body):
                return shipped, False
            with self.db:
                self.db.executemany(SHIPPED, [(name, event_id) for event_id, _ in batch])
            shipped += len(batch)
        return shipped, True

    def recent(self, name, now_ms):
        return [json.loads(row) for (row,) in self.db.execute(RECENT, (name, now_ms - DAY_MS))]

    def close(self):
        self.db.close()
