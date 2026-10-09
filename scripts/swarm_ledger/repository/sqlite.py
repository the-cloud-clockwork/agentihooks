import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager, suppress
from pathlib import Path

from .events import append_events, read_events, write_events
from .rows import TABLES, assemble, diff, encode, flatten, read_rows, write_rows
from .seeds import read_seeds, sync_values, write_seeds

SCHEMA = """
CREATE TABLE IF NOT EXISTS registry (slug TEXT, path TEXT, value TEXT, PRIMARY KEY(slug,path));
CREATE TABLE IF NOT EXISTS ledgers (slug TEXT PRIMARY KEY, revision INTEGER NOT NULL, generation INTEGER NOT NULL, token TEXT NOT NULL, summary TEXT NOT NULL, touched_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS revisions (slug TEXT, revision TEXT, position INTEGER, PRIMARY KEY(slug,revision));
CREATE TABLE IF NOT EXISTS seed_base (slug TEXT, path TEXT, value TEXT, PRIMARY KEY(slug,path));
CREATE TABLE IF NOT EXISTS seed_deltas (slug TEXT, revision TEXT, path TEXT, value TEXT, PRIMARY KEY(slug,revision,path));
CREATE TABLE IF NOT EXISTS events (slug TEXT, key TEXT, revision INTEGER, position INTEGER, value TEXT, PRIMARY KEY(slug,key));
CREATE INDEX IF NOT EXISTS events_revision ON events(slug,revision);
CREATE INDEX IF NOT EXISTS events_position ON events(slug,position);
"""
DATABASE = "ledgers.sqlite3"
BEGIN = "BEGIN"
REGISTRY = "SELECT path,value FROM registry WHERE slug=?"
PATH_END = "\x7f"
PER_LEDGER = (*TABLES, "ledgers", "revisions", "seed_base", "seed_deltas", "events")
GENERATION_SHIFT = 20
BEGIN_IMMEDIATE = "BEGIN IMMEDIATE"
STORED = "SELECT 1 FROM ledgers WHERE slug=?"
GENERATION = "SELECT generation FROM ledgers WHERE slug=?"
TOKEN = "SELECT token FROM ledgers WHERE slug=?"
SUMMARIES = "SELECT summary FROM ledgers ORDER BY touched_at DESC, slug"
SLUGS = "SELECT slug FROM ledgers ORDER BY slug"
ROOT = "[]"
REGISTRY_TABLE = "registry"
UPDATE = "UPDATE ledgers SET revision=?, generation=?, summary=?, touched_at=? WHERE slug=?"
INSERT = "INSERT INTO ledgers VALUES (?, ?, ?, ?, ?, ?)"
SUMMARY_KEYS = ("slug", "title", "overview", "closed_at", "size", "open", "done", "updated_at")


class Missing(KeyError, ValueError):
    pass


class Entry:
    """One ledger as last stored: its generation, canonical text and a parsed copy no caller receives."""

    def __init__(self, generation, text, state):
        self.generation, self.text, self.state = generation, text, state


def summarize(slug: str, state: dict) -> dict:
    import ledger_bin
    import ledger_close
    import ledger_size

    items = [i for i in state.get("tasks") or state.get("phases") or [] if not i.get("out_of_scope")]
    done = sum(1 for i in items if i.get("done") is True)
    meta = state.get("_meta", {})
    return {
        "slug": slug,
        "title": state.get("title") or slug,
        "overview": ledger_close.intro(state.get("overview") or ""),
        "closed_at": state.get("closed_at"),
        "size": ledger_size.size_of(state),
        "open": len(items) - done,
        "done": done,
        "updated_at": meta.get("updated_at"),
        "created_at": meta.get("created_at"),
        "finished": ledger_bin.finished(state),
    }


def scope(parts: list) -> tuple[list, str]:
    """The exact ancestor paths of `parts` and the prefix every descendant path starts with."""
    return [encode(parts[:depth]) for depth in range(len(parts) + 1)], encode(parts)[:-1] + ","


def read_partial(connection, slug: str, keys: tuple) -> dict:
    """Rows under each key path only, assembled; `_meta.events` is filled from the events table."""
    rows = {}
    for parts in keys:
        exact, prefix = scope(parts)
        marks = ",".join("?" * len(exact))
        for table in TABLES:
            for path, parent, key, position, kind, value in connection.execute(
                f"SELECT path, parent, key, position, kind, value FROM {table} "
                f"WHERE slug=? AND (path IN ({marks}) OR (path > ? AND path < ?))",
                (slug, *exact, prefix, prefix + PATH_END),
            ):
                rows[path] = (table, parent, key, position, kind, value)
    if "[]" not in rows:
        raise Missing(slug)
    state = assemble(rows)
    meta = state.get("_meta")
    if isinstance(meta, dict) and "events" in meta:
        meta["events"] = read_events(connection, slug)
    return state


def key_parts(key: str) -> list:
    """`tasks`, `_meta.members` or `tasks/t1` as stored path parts."""
    name, _, item = key.partition("/")
    parts = name.split(".")
    return [*parts, ["id", item, 0]] if item else parts


@contextmanager
def read_only(directory):
    """A read only connection to the folder's database, or None when it holds none."""
    path = Path(directory) / DATABASE
    if not path.exists():
        yield None
        return
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        yield connection
    finally:
        connection.close()


def read_ledger(directory, slug: str, *keys: str) -> dict | None:
    """Named parts of a stored ledger through a read only connection, for hooks; None when it is not stored."""
    with read_only(directory) as connection:
        if connection is None:
            return None
        try:
            with connection:
                connection.execute(BEGIN)
                return read_partial(connection, slug, tuple(key_parts(key) for key in keys))
        except (Missing, sqlite3.OperationalError):
            return None


def read_registry(directory, name: str) -> dict:
    with read_only(directory) as connection:
        if connection is None:
            return {}
        try:
            return {key: json.loads(value) for key, value in connection.execute(REGISTRY, (name,))}
        except sqlite3.OperationalError:
            return {}


def read_ids(directory, slug: str, collection: str) -> tuple:
    """The ids of a stored collection in order, read from its row paths alone."""
    parent = encode([collection])
    with read_only(directory) as connection:
        if connection is None:
            return ()
        try:
            rows = [
                row
                for table in TABLES
                for row in connection.execute(
                    f"SELECT position, path FROM {table} WHERE slug=? AND parent=?", (slug, parent)
                )
            ]
        except sqlite3.OperationalError:
            return ()
    found = (json.loads(path)[1] for _, path in sorted(rows))
    return tuple(part[1] for part in found if part[0] == "id")


def read_slugs(directory) -> list:
    with read_only(directory) as connection:
        if connection is None:
            return []
        try:
            return [slug for (slug,) in connection.execute(SLUGS)]
        except sqlite3.OperationalError:
            return []


def read_ledgers(directory, *keys: str) -> dict:
    """One snapshot; a slug whose root row is missing is left out."""
    parts = tuple(key_parts(key) for key in keys)
    with read_only(directory) as connection:
        if connection is None:
            return {}
        found = {}
        try:
            with connection:
                connection.execute(BEGIN)
                for (slug,) in connection.execute(SLUGS).fetchall():
                    with suppress(Missing):
                        found[slug] = read_partial(connection, slug, parts)
        except sqlite3.OperationalError:
            return {}
        return found


def read_document(directory, slug: str) -> dict | None:
    """A whole stored ledger with its events, for explicit full reads such as a recall backfill."""
    with read_only(directory) as connection:
        if connection is None:
            return None
        try:
            with connection:
                connection.execute(BEGIN)
                rows = read_rows(connection, slug)
                if ROOT not in rows:
                    return None
                state = assemble(rows)
                if "events" in state["_meta"]:
                    state["_meta"]["events"] = read_events(connection, slug)
                return state
        except sqlite3.OperationalError:
            return None


def without_events(state: dict) -> dict:
    """The document as its rows hold it: the event log lives in its own table."""
    return {**state, "_meta": {key: value for key, value in state["_meta"].items() if key != "events"}}


class SQLiteLedgerRepository:
    """The durable ledger record: one row per JSON value, written only where a mutation changed it."""

    def __init__(self, path: Path | None = None, domain=None):
        self._path = None if path is None else Path(path)
        self._domain = domain
        self.trace = None
        self._ready = set()
        self._cache = {}
        self._loads = {}
        self._guard = threading.Lock()

    @property
    def domain(self):
        if self._domain is None:
            import ledger_core

            self._domain = ledger_core
        return self._domain

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else self.domain.LEDGER_DIR / DATABASE

    @property
    def directory(self) -> Path:
        return self.path.parent

    def bound(self, domain) -> "SQLiteLedgerRepository":
        """This repository, or one sharing its cache that reads its folder and rules from another copy of the domain."""
        if domain is self.domain or self._path is not None:
            return self
        twin = SQLiteLedgerRepository(domain=domain)
        twin._cache, twin._loads, twin._ready, twin._guard = self._cache, self._loads, self._ready, self._guard
        return twin

    @contextmanager
    def connect(self):
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30)
        try:
            if path not in self._ready:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(SCHEMA)
                for table in TABLES:
                    connection.execute(
                        f"CREATE TABLE IF NOT EXISTS {table} (slug TEXT, path TEXT, parent TEXT, key TEXT, "
                        "position INTEGER, kind TEXT, value TEXT, PRIMARY KEY(slug,path))"
                    )
                self._ready.add(path)
            connection.execute("PRAGMA synchronous=FULL")
            connection.set_trace_callback(self.trace)
            yield connection
        finally:
            connection.close()

    def _key(self, slug):
        return str(self.path), slug

    def _entry(self, connection, slug: str) -> Entry:
        """The stored ledger; a reader whose snapshot predates a cached write gets that newer committed copy."""
        row = connection.execute(GENERATION, (slug,)).fetchone()
        if row is None:
            raise Missing(slug)
        cached = self._cache.get(self._key(slug))
        if cached is not None and cached.generation >= row[0]:
            return cached
        with self._loading(slug):
            cached = self._cache.get(self._key(slug))
            if cached is not None and cached.generation >= row[0]:
                return cached
            state = assemble(read_rows(connection, slug))
            if "events" in state["_meta"]:
                state["_meta"]["events"] = read_events(connection, slug)
            entry = Entry(row[0], encode(state), state)
            self._remember(slug, entry)
        return entry

    def _loading(self, slug: str) -> threading.Lock:
        with self._guard:
            return self._loads.setdefault(self._key(slug), threading.Lock())

    def _remember(self, slug: str, entry: Entry) -> None:
        with self._guard:
            current = self._cache.get(self._key(slug))
            if current is None or current.generation < entry.generation:
                self._cache[self._key(slug)] = entry

    def _adopt(self, slug: str | None = None) -> None:
        from . import legacy

        legacy.adopt(self, slug)

    def exists(self, slug: str) -> bool:
        self._adopt(slug)
        with self.connect() as connection:
            return connection.execute(STORED, (slug,)).fetchone() is not None

    def get_document(self, slug: str) -> dict:
        self._adopt(slug)
        with self.connect() as connection, connection:
            connection.execute(BEGIN)
            return json.loads(self._entry(connection, slug).text)

    def read(self, slug: str, *keys: str) -> dict:
        """Only the named parts of a ledger, such as `overview`, `_meta.members` or `tasks/t1`."""
        self._adopt(slug)
        with self.connect() as connection, connection:
            connection.execute(BEGIN)
            return read_partial(connection, slug, tuple(key_parts(key) for key in keys))

    def token(self, slug: str) -> str | None:
        self._adopt(slug)
        with self.connect() as connection:
            row = connection.execute(TOKEN, (slug,)).fetchone()
        return None if row is None else row[0]

    def apply_ops(
        self, slug: str, changes: list | None = None, ops: list | None = None, gate=None
    ) -> tuple[dict, list]:
        from . import mutation

        self._adopt(slug)
        with self.domain.LOCK, self.connect() as connection:
            with connection:
                connection.execute(BEGIN_IMMEDIATE)
                entry = self._entry(connection, slug)
                state = json.loads(entry.text)
                rejected, ctx = mutation.apply(slug, state, self.domain, changes, ops, gate)
                written = self._write(connection, slug, entry, state, ctx.events if ctx.changed else [])
            self._remember(slug, written)
        return json.loads(written.text), rejected

    def _write(self, connection, slug: str, entry: Entry, state: dict, events: list) -> Entry:
        before, after = diff(*(without_events(document) for document in (entry.state, state)))
        write_rows(connection, slug, before, after)
        append_events(connection, slug, events, self.domain.EVENTS_KEPT)
        generation = entry.generation + 1
        connection.execute(
            UPDATE,
            (state["_meta"]["rev"], generation, encode(summarize(slug, state)), self.domain.now_ms(), slug),
        )
        return Entry(generation, encode(state), state)

    def _insert(self, connection, slug: str, state: dict, token: str, seeds: dict | None = None) -> None:
        previous = connection.execute(GENERATION, (slug,)).fetchone()
        now = self.domain.now_ms()
        generation = now << GENERATION_SHIFT if previous is None else max(now << GENERATION_SHIFT, previous[0] + 1)
        for table in PER_LEDGER:
            connection.execute(f"DELETE FROM {table} WHERE slug=?", (slug,))
        stored = {**state, "_meta": {**state["_meta"]}}
        events = stored["_meta"].get("events", [])
        if "events" in stored["_meta"]:
            stored["_meta"]["events"] = []
        write_rows(connection, slug, {}, flatten(stored))
        write_events(connection, slug, events)
        write_seeds(connection, slug, seeds or {})
        connection.execute(
            INSERT,
            (
                slug,
                state["_meta"]["rev"],
                generation,
                token,
                encode(summarize(slug, state)),
                now,
            ),
        )
        self._cache.pop(self._key(slug), None)

    def create_document(
        self, slug: str, doc: dict, meta: dict, token: str | None = None, replace: bool = False
    ) -> bool:
        """Store a ledger built from content: its first sync derives priorities, notifications and rev 1."""
        from . import mutation

        with self.domain.LOCK, self.connect() as connection, connection:
            connection.execute(BEGIN_IMMEDIATE)
            if not replace and connection.execute(STORED, (slug,)).fetchone():
                return False
            state = {**doc, "_meta": meta}
            mutation.apply(slug, state, self.domain, created=True)
            self._insert(connection, slug, state, token or secrets.token_urlsafe(24))
            return True

    def import_document(self, slug: str, state: dict, token: str | None = None, replace: bool = False) -> None:
        """Store an exported or legacy document as it is, then prove its export equals it."""
        stored = json.loads(encode(state))
        seeds = stored["_meta"].get("seeds")
        if seeds is not None:
            stored["_meta"]["seeds"] = {}
        with self.domain.LOCK, self.connect() as connection, connection:
            connection.execute(BEGIN_IMMEDIATE)
            if not replace and connection.execute(STORED, (slug,)).fetchone():
                raise ValueError(f"ledger {slug} exists; import refuses to replace it")
            self._insert(connection, slug, stored, token or secrets.token_urlsafe(24), seeds)
            if self._export(connection, slug) != json.loads(encode(state)):
                self._cache.pop(self._key(slug), None)
                raise ValueError(f"imported ledger {slug} does not export to its source document")

    def _export(self, connection, slug: str) -> dict:
        state = json.loads(self._entry(connection, slug).text)
        if "seeds" in state["_meta"]:
            state["_meta"]["seeds"] = read_seeds(connection, slug)
        return state

    def export_document(self, slug: str) -> dict:
        """The complete stored document, seeds included: the explicit interchange read."""
        self._adopt(slug)
        with self.connect() as connection, connection:
            connection.execute(BEGIN)
            return self._export(connection, slug)

    def events_since(self, slug: str, revision: int) -> list:
        self._adopt(slug)
        with self.connect() as connection:
            if connection.execute(STORED, (slug,)).fetchone() is None:
                raise Missing(slug)
            return read_events(connection, slug, revision)

    def summaries(self, connection=None) -> list:
        """Every stored ledger's summary with its bin facts, newest write first."""
        if connection is None:
            self._adopt()
            with self.connect() as connection:
                return self.summaries(connection)
        return [json.loads(summary) for (summary,) in connection.execute(SUMMARIES)]

    def list_summaries(self) -> list:
        return [{key: summary[key] for key in SUMMARY_KEYS} for summary in self.summaries()]

    def purge(self, slug: str, connection) -> None:
        for table in PER_LEDGER:
            connection.execute(f"DELETE FROM {table} WHERE slug=?", (slug,))
        self._cache.pop(self._key(slug), None)

    def registry(self, name: str, connection) -> dict:
        return {key: json.loads(value) for key, value in connection.execute(REGISTRY, (name,))}

    def save_registry(self, connection, name: str, entries: dict) -> None:
        sync_values(connection, REGISTRY_TABLE, name, {key: encode(value) for key, value in entries.items()})

    def create(self, slug: str, content: dict, size: str = "small") -> bool:
        from . import legacy

        return legacy.create(self, slug, content, size)

    def delete(self, slug: str, now: int | None = None) -> None:
        from . import bin_storage

        bin_storage.delete(slug, now)

    def restore(self, slug: str, now: int | None = None) -> bool:
        from . import bin_storage

        return bin_storage.restore(slug, now)
