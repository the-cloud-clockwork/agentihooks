import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from types import ModuleType

from . import hierarchy, refresh
from .sqlite import BEGIN, BEGIN_IMMEDIATE, ROWS_SCHEMA, SCHEMA, TABLES_SQL, Snapshot, SQLiteLedgerRepository

URL = "AGENTIHOOKS_SWARM_DATABASE_URL"
WRITER_LOCK = 0x4C454447
KEPT = 8
LOCK_WAIT = 10
LOCK_TRIES = 3
LOCK_REFUSED = f"the ledger writer lock was not granted in {LOCK_TRIES} waits of {LOCK_WAIT} seconds"
SESSION = {
    "options": f"-c lock_timeout={LOCK_WAIT}s -c idle_in_transaction_session_timeout=15s -c statement_timeout=60s",
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}
READ_SNAPSHOT = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
# the first start after an upgrade builds row indexes over every stored ledger
SCHEMA_STATEMENTS_UNTIMED = "SET LOCAL statement_timeout = 0"
TABLES = "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()"
STATEMENTS = {
    BEGIN_IMMEDIATE: f"SELECT pg_advisory_xact_lock({WRITER_LOCK})",
    TABLES_SQL: TABLES,
    "SELECT 1 FROM sqlite_master WHERE name='hosted_fences'": f"{TABLES} AND table_name='hosted_fences'",
}
REWRITES = (
    (
        hierarchy.ORDER,
        "((CASE n.kind WHEN 'plan' THEN 0 WHEN 'phase' THEN 1 WHEN 'slice' THEN 2 ELSE 3 END)::text"
        " || '.' || lpad(n.position::text, 9, '0'))",
    ),
    ("n.parent_id IS :node", "n.parent_id IS NOT DISTINCT FROM :node"),
    (":node IS NOT NULL FROM", "CASE WHEN :node IS NULL THEN 0 ELSE 1 END FROM"),
    ("CROSS JOIN work_nodes n ON", "JOIN work_nodes n ON"),
    ("GROUP BY n.node_id ", "GROUP BY n.node_id, n.kind, n.parent_id, n.position "),
)
IGNORE = "INSERT OR IGNORE INTO "
UNFENCEABLE = "PRAGMA table_info"
NAMED = re.compile(r"(?<![:\w]):(\w+)")


def url(env: Mapping[str, str]) -> str | None:
    return env.get(URL) or None


def translate(sql: str, named: bool = False) -> str:
    """The Postgres text of a statement the ledger repository writes for SQLite, with psycopg placeholders."""
    statement = STATEMENTS.get(sql, sql)
    for old, new in REWRITES:
        statement = statement.replace(old, new)
    if statement.startswith(IGNORE):
        statement = f"INSERT INTO {statement.removeprefix(IGNORE)} ON CONFLICT DO NOTHING"
    statement = statement.replace("%", "%%")
    return NAMED.sub(r"%(\1)s", statement) if named else statement.replace("?", "%s")


def ddl(script: str) -> list:
    """The SQLite schema as Postgres statements: 64 bit integers and byte ordered text, as SQLite compares them."""
    script = re.sub(r"\bINTEGER\b", "BIGINT", script)
    script = re.sub(r"\bTEXT\b", 'TEXT COLLATE "C"', script)
    return [statement.strip() for statement in script.split(";") if statement.strip()]


class Empty:
    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


def errors() -> ModuleType:
    import psycopg2
    import psycopg2.errors
    import psycopg2.extras

    return psycopg2


@contextmanager
def mapped() -> Iterator[None]:
    """Raise Postgres failures as the sqlite3 errors every ledger caller already handles."""
    psycopg2 = errors()
    try:
        yield
    except psycopg2.IntegrityError as exc:
        raise sqlite3.IntegrityError(str(exc)) from exc
    except psycopg2.errors.UndefinedTable as exc:
        raise sqlite3.OperationalError(str(exc)) from exc
    except psycopg2.Error as exc:
        raise sqlite3.DatabaseError(str(exc)) from exc


class Pool:
    """Idle connections to one database, at most KEPT of them, each rolled back before it is kept."""

    def __init__(self, address: str):
        self.address, self.idle, self.lock = address, [], threading.Lock()

    def take(self) -> object:
        with self.lock:
            if self.idle:
                return self.idle.pop()
        with mapped():
            return errors().connect(self.address, **SESSION)

    def give(self, raw) -> None:
        psycopg2 = errors()
        try:
            raw.rollback()
        except psycopg2.Error:
            raw.close()
            return
        with self.lock:
            if not raw.closed and len(self.idle) < KEPT:
                self.idle.append(raw)
                return
        raw.close()


POOLS = {}
POOLS_LOCK = threading.Lock()


def pool(address: str) -> Pool:
    with POOLS_LOCK:
        return POOLS.setdefault(address, Pool(address))


class Connection:
    """A pooled Postgres connection answering the sqlite3 calls the ledger repository makes."""

    def __init__(self, address: str):
        self.pool = pool(address)
        self.raw = self.pool.take()

    def execute(self, sql: str, parameters: tuple | Mapping = ()) -> object:
        if sql.startswith(UNFENCEABLE):
            raise sqlite3.OperationalError("hosted fences belong to SQLite ledger folders")
        if sql.startswith("PRAGMA"):
            return Empty()
        if sql == BEGIN_IMMEDIATE:
            return self.lock()
        with mapped():
            if sql == BEGIN:
                if self.raw.get_transaction_status() != errors().extensions.TRANSACTION_STATUS_IDLE:
                    raise sqlite3.OperationalError("cannot start a transaction within a transaction")
                sql = READ_SNAPSHOT
            cursor = self.raw.cursor()
            cursor.execute(translate(sql, isinstance(parameters, Mapping)), parameters)
        return cursor

    def lock(self) -> object:
        """The writer lock, asked for again after each wait the server ends at its lock timeout."""
        psycopg2 = errors()
        lost = psycopg2.errors.LockNotAvailable
        with mapped():
            if self.raw.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                raise sqlite3.OperationalError("cannot start a transaction within a transaction")
            for _ in range(LOCK_TRIES):
                cursor = self.raw.cursor()
                try:
                    cursor.execute(translate(BEGIN_IMMEDIATE), ())
                    return cursor
                except lost:
                    self.raw.rollback()
        raise sqlite3.OperationalError(LOCK_REFUSED)

    def executemany(self, sql: str, rows: Iterable) -> None:
        with mapped(), self.raw.cursor() as cursor:
            errors().extras.execute_batch(cursor, translate(sql), list(rows))

    def executescript(self, script: str) -> None:
        for statement in ddl(script):
            self.execute(statement)

    def commit(self) -> None:
        with mapped():
            self.raw.commit()

    def rollback(self) -> None:
        with mapped():
            self.raw.rollback()

    def close(self) -> None:
        self.pool.give(self.raw)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback) -> bool:
        if kind is None:
            self.commit()
        else:
            self.rollback()
        return False


class Database:
    def __init__(self, address: str):
        self.address = address

    @contextmanager
    def read_only(self) -> Iterator["Connection"]:
        connection = Connection(self.address)
        try:
            yield connection
        finally:
            connection.close()


class PostgresLedgerRepository(SQLiteLedgerRepository):
    adopts = False

    def __init__(self, address: str, domain=None):
        super().__init__(None, domain)
        self.address = address

    def bound(self, domain) -> "PostgresLedgerRepository":
        if domain is self.domain:
            return self
        twin = PostgresLedgerRepository(self.address, domain)
        twin._cache, twin._loads, twin._ready, twin._guard = self._cache, self._loads, self._ready, self._guard
        return twin

    def _key(self, slug):
        return self.address, slug

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        connection = Connection(self.address)
        try:
            if self.address not in self._ready:
                self._set_up(connection, self.address)
            yield connection
        finally:
            connection.close()

    def _set_up(self, connection, address) -> None:
        with self._guard:
            if address in self._ready:
                return
            with connection:
                connection.execute(BEGIN_IMMEDIATE)
                connection.execute(SCHEMA_STATEMENTS_UNTIMED)
                connection.executescript(SCHEMA + hierarchy.SCHEMA + ROWS_SCHEMA + refresh.SCHEMA)
            self._ready.add(address)

    @contextmanager
    def snapshot(self) -> Iterator[Snapshot]:
        with self.connect() as connection, connection:
            connection.execute(BEGIN)
            yield Snapshot(self, connection)
