import json
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager, suppress
from pathlib import Path

from scripts.swarm_ledger import request_metrics

from . import artifact_files, heap, hierarchy, media_files, outbox, refresh
from .entry import Entry
from .events import append_events, read_events, trim_events, write_events
from .fragments import document
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
CREATE TABLE IF NOT EXISTS relay_journal (kind TEXT, slug TEXT, revision INTEGER, PRIMARY KEY(kind,slug,revision));
CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, slug TEXT NOT NULL, revision INTEGER NOT NULL, subject TEXT NOT NULL, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox_sent (id TEXT PRIMARY KEY, sent_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS artifact_files (slug TEXT, id TEXT, data TEXT, PRIMARY KEY(slug,id));
CREATE TABLE IF NOT EXISTS media_files (slug TEXT, id TEXT, data TEXT, saved_at INTEGER, PRIMARY KEY(slug,id));
"""
ROWS_SCHEMA = "".join(
    f"CREATE TABLE IF NOT EXISTS {table} (slug TEXT, path TEXT, parent TEXT, key TEXT, "
    "position INTEGER, kind TEXT, value TEXT, PRIMARY KEY(slug,path));\n"
    for table in TABLES
)
DATABASE = "ledgers.sqlite3"
BEGIN = "BEGIN"
REGISTRY = "SELECT path,value FROM registry WHERE slug=?"
PATH_END = "\x7f"
PER_LEDGER = (*TABLES, "ledgers", "revisions", "seed_base", "seed_deltas", "events", "row_changes")
GENERATION_SHIFT = 20
CHANGES_KEPT = 256
BEGIN_IMMEDIATE = "BEGIN IMMEDIATE"
STORED = "SELECT 1 FROM ledgers WHERE slug=?"
GENERATION = "SELECT generation FROM ledgers WHERE slug=?"
TOKEN = "SELECT token FROM ledgers WHERE slug=?"
SUMMARIES = "SELECT summary FROM ledgers ORDER BY touched_at DESC, slug"
SLUGS = "SELECT slug FROM ledgers ORDER BY slug"
ROOT = refresh.ROOT
REGISTRY_TABLE = "registry"
UPDATE = "UPDATE ledgers SET revision=?, generation=?, summary=?, touched_at=? WHERE slug=?"
INSERT = "INSERT INTO ledgers VALUES (?, ?, ?, ?, ?, ?)"
SUMMARY_KEYS = ("slug", "title", "overview", "closed_at", "size", "open", "done", "updated_at")
HELD = "SELECT kind, slug, revision FROM relay_journal ORDER BY kind, slug, revision"
HOLD = "INSERT OR IGNORE INTO relay_journal VALUES (?, ?, ?)"
RELEASE = "DELETE FROM relay_journal WHERE kind=? AND slug=? AND revision BETWEEN ? AND ?"
DROP = "DELETE FROM relay_journal WHERE kind=? AND slug=?"
REGISTRIES = "SELECT slug,path,value FROM registry WHERE path=? ORDER BY slug,path"
ADD_REGISTRY = "INSERT INTO registry VALUES (?,?,?)"
DROP_REGISTRIES = "DELETE FROM registry WHERE path=?"
TABLES_SQL = "SELECT name FROM sqlite_master WHERE type='table'"
FENCES = "CREATE TABLE IF NOT EXISTS hosted_fences (slug TEXT PRIMARY KEY, destination TEXT NOT NULL)"
FENCED = "SELECT destination FROM hosted_fences WHERE slug IN (?, '*')"
UNFENCE = "DELETE FROM hosted_fences WHERE destination=?"
FROZEN = "SELECT 1 FROM hosted_fences WHERE slug='*'"
FENCE_DESTINATIONS = "SELECT destination FROM hosted_fences WHERE slug IN (?, '*') OR ?='*'"
TABLE_ID = re.compile(r"[a-z_]+\Z")
# a fenced ledger's last relay must still be able to hold its revision
UNFENCED = ("hosted_fences", "relay_journal")


class Missing(KeyError, ValueError):
    pass


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
    if hasattr(directory, "read_only"):
        with directory.read_only() as connection:
            yield connection
        return
    path = Path(directory) / DATABASE
    if not path.exists():
        yield None
        return
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
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
            with connection:
                connection.execute(BEGIN)
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


def registry_rows(connection, slug: str) -> list:
    return [list(row) for row in connection.execute(REGISTRIES, (slug,))]


def without_events(state: dict) -> dict:
    """The document as its rows hold it: the event log lives in its own table."""
    return {**state, "_meta": {key: value for key, value in state["_meta"].items() if key != "events"}}


def fenced(directory: Path, slug: str) -> bool:
    """Whether the hosted migration fenced this ledger, or every ledger, in the folder's database."""
    return fence_row(directory, FENCED, (slug,))


def frozen(directory: Path) -> bool:
    return fence_row(directory, FROZEN, ())


def fence_row(directory: Path, query: str, parameters: tuple) -> bool:
    with read_only(directory) as connection:
        exists = (
            connection is not None
            and connection.execute("SELECT 1 FROM sqlite_master WHERE name='hosted_fences'").fetchone()
        )
        return bool(exists and connection.execute(query, parameters).fetchone())


class Transaction:
    """One immediate write transaction on the store, held under the core lock."""

    def __init__(self, repository: "SQLiteLedgerRepository", connection):
        self.repository, self.connection, self.written = repository, connection, {}

    def exists(self, slug: str) -> bool:
        return self.connection.execute(STORED, (slug,)).fetchone() is not None

    def entry(self, slug: str) -> Entry:
        return self.repository._entry(self.connection, slug)

    def hierarchy(self, slug: str) -> tuple[dict, set]:
        return hierarchy.stored(self.connection, slug)

    def write(self, slug: str, entry: Entry, state: dict, events: list) -> Entry:
        written = self.written[slug] = self.repository._write(self.connection, slug, entry, state, events)
        return written

    def insert(self, slug: str, state: dict, seeds: dict | None, token: str) -> None:
        self.repository._insert(self.connection, slug, state, token, seeds)

    def add_registries(self, rows: list) -> None:
        self.connection.executemany(ADD_REGISTRY, rows)

    def replace_registries(self, slug: str, rows: list) -> None:
        self.connection.execute(DROP_REGISTRIES, (slug,))
        self.add_registries(rows)

    def registries(self, slug: str) -> list:
        return registry_rows(self.connection, slug)

    def purge(self, slug: str) -> None:
        self.repository.purge(slug, self.connection)
        self.connection.execute(DROP_REGISTRIES, (slug,))

    def export(self, slug: str) -> dict:
        return self.repository._export(self.connection, slug)

    def prepare_fence(self, slug: str, destination: str) -> None:
        connection = self.connection
        connection.execute(FENCES)
        previous = connection.execute(FENCE_DESTINATIONS, (slug, slug)).fetchall()
        if any(row[0] != destination for row in previous):
            raise ValueError("ledger already has a different hosted destination")
        if slug != "*" and not self.exists(slug):
            raise ValueError("no such ledger")
        for (table,) in connection.execute(TABLES_SQL).fetchall():
            if table in UNFENCED or not TABLE_ID.fullmatch(table):
                continue
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "slug" not in columns:
                continue
            column = "path" if table == "registry" else "slug"
            for action, reference in (("INSERT", "NEW"), ("UPDATE", "OLD"), ("DELETE", "OLD")):
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS hosted_{table}_{action} BEFORE {action} ON {table} "
                    f"WHEN EXISTS(SELECT 1 FROM hosted_fences WHERE slug IN ({reference}.{column}, '*')) "
                    "BEGIN SELECT RAISE(ABORT,'ledger moved to hosted service'); END"
                )

    def fence(self, slug: str, destination: str) -> None:
        self.connection.execute("INSERT OR IGNORE INTO hosted_fences VALUES (?,?)", (slug, destination))
        self.connection.commit()

    def unfence(self, destination: str) -> None:
        self.connection.execute(FENCES)
        self.connection.execute(UNFENCE, (destination,))


class Snapshot:
    """One read only snapshot of the stored folder."""

    def __init__(self, repository: "SQLiteLedgerRepository", connection):
        self.repository, self.connection = repository, connection

    def slugs(self) -> list:
        return [slug for (slug,) in self.connection.execute(SLUGS)]

    def document(self, slug: str) -> dict:
        """The ledger as its rows hold it, without its event log."""
        rows = read_rows(self.connection, slug)
        if not rows:
            raise Missing(slug)
        return assemble(rows)

    def hierarchy(self, slug: str) -> tuple[dict, set]:
        tables = {row[0] for row in self.connection.execute(TABLES_SQL)}
        nodes = (
            {
                node: (kind, parent, position)
                for node, kind, parent, position in self.connection.execute(hierarchy.NODES, (slug,))
            }
            if "work_nodes" in tables
            else {}
        )
        dependencies = (
            set(self.connection.execute(hierarchy.DEPENDENCIES, (slug,))) if "work_dependencies" in tables else set()
        )
        return nodes, dependencies

    def export(self, slug: str) -> dict:
        return self.repository._export(self.connection, slug)

    def registries(self, slug: str) -> list:
        return registry_rows(self.connection, slug)

    def token(self, slug: str) -> str | None:
        row = self.connection.execute(TOKEN, (slug,)).fetchone()
        return None if row is None else row[0]


class SQLiteLedgerRepository:
    """The durable ledger record: one row per JSON value, written only where a mutation changed it."""

    adopts = True

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
            connection.execute("PRAGMA foreign_keys=ON")
            if path not in self._ready:
                self._set_up(connection, path)
            connection.execute("PRAGMA synchronous=FULL")
            connection.set_trace_callback(self.trace)
            yield connection
        finally:
            connection.close()

    def _set_up(self, connection, path: Path) -> None:
        with self._guard:
            if path in self._ready:
                return
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            connection.executescript(hierarchy.SCHEMA)
            connection.executescript(ROWS_SCHEMA)
            connection.executescript(refresh.SCHEMA)
            outbox.migrate(connection)
            self._ready.add(path)

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
        with self._guard:
            loading = self._loads.setdefault(self._key(slug), threading.Lock())
        with loading:
            cached = self._cache.get(self._key(slug))
            if cached is not None and cached.generation >= row[0]:
                return cached
            state = None if cached is None else self._refreshed(connection, slug, cached, row[0])
            whole = state is None
            if whole:
                state = assemble(read_rows(connection, slug))
            if "events" in state["_meta"]:
                state["_meta"] = {**state["_meta"], "events": read_events(connection, slug)}
            fragments = document(state, None if whole else cached.fragments)
            entry = Entry(row[0], None, fragments.value, fragments)
            self._remember(slug, entry)
        if whole:
            heap.release()
        return entry

    def _refreshed(self, connection, slug: str, cached: Entry, generation: int) -> dict | None:
        paths = refresh.changed_paths(connection, slug, cached.generation, generation, CHANGES_KEPT)
        return None if paths is None else refresh.refreshed(connection, slug, cached.state, paths)

    def _remember(self, slug: str, entry: Entry) -> None:
        with self._guard:
            current = self._cache.get(self._key(slug))
            if current is None or current.generation < entry.generation:
                self._cache[self._key(slug)] = entry

    def _cached(self) -> dict:
        with self._guard:
            return {key[1]: entry for key, entry in self._cache.items() if key[0] == str(self.path)}

    def generations(self) -> dict:
        """{slug: generation} of every cached ledger."""
        return {slug: entry.generation for slug, entry in self._cached().items()}

    def carry(self, since: dict) -> dict:
        """{slug: [generation, text]} of the cached ledgers whose generation moved past since."""
        return {
            slug: [entry.generation, entry.text]
            for slug, entry in self._cached().items()
            if since.get(slug) != entry.generation
        }

    def adopt_carried(self, carried: dict) -> None:
        """Cache the committed copies another process carried over, unless a copy at least as new is already cached."""
        held = self.generations()
        for slug, (generation, text) in carried.items():
            if slug not in held or held[slug] < generation:
                fragments = document(json.loads(text))
                self._remember(slug, Entry(generation, None, fragments.value, fragments))

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
            return self._entry(connection, slug).mutable()

    def current(self, slug: str) -> Entry:
        """The stored entry: its generation, text and a parsed copy that callers only read."""
        self._adopt(slug)
        with self.connect() as connection, connection:
            connection.execute(BEGIN)
            return self._entry(connection, slug)

    def encoded_view(self, slug: str, view) -> str:
        """view(state) of the stored ledger, built once per generation and keyed by view, so view is a module
        level function that never changes the state it reads."""
        self._adopt(slug)
        with self.connect() as connection, connection:
            connection.execute(BEGIN)
            entry = self._entry(connection, slug)
        if view not in entry.views:
            entry.views[view] = view(entry.state)
            heap.release()
        return entry.views[view]

    def export_since(self, slug: str, since: int, view, changes) -> tuple[int, str]:
        """(generation, text) of the stored ledger: changes(state, paths) when the database recorded every row
        change after generation since, else view(state) as encoded_view builds it."""
        self._adopt(slug)
        with self.connect() as connection, connection:
            connection.execute(BEGIN)
            entry = self._entry(connection, slug)
            paths = (
                refresh.changed_paths(connection, slug, since, entry.generation, CHANGES_KEPT)
                if 0 < since <= entry.generation
                else None
            )
        text = None if paths is None else changes(entry.state, paths)
        if text is not None:
            return entry.generation, text
        if view not in entry.views:
            entry.views[view] = view(entry.state)
            heap.release()
        return entry.generation, entry.views[view]

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
        self, slug: str, changes: list | None = None, ops: list | None = None, gate=None, *, copy_state: bool = True
    ) -> tuple[dict, list]:
        from . import mutation

        self._adopt(slug)
        with request_metrics.locked(self.domain.LOCK), self.connect() as connection:
            with connection:
                request_metrics.begin(connection, BEGIN_IMMEDIATE)
                entry = self._entry(connection, slug)
                state = entry.mutable()
                rejected, ctx = mutation.apply(slug, state, self.domain, changes, ops, gate)
                written = self._write(connection, slug, entry, state, ctx.events if ctx.changed else [])
            artifact_files.drop(slug, ctx.unstored)
            media_files.drop(slug, ctx.unstored)
            self._remember(slug, written)
        return (written.mutable() if copy_state else written.state), rejected

    def _write(self, connection, slug: str, entry: Entry, state: dict, events: list) -> Entry:
        meta, previous = state["_meta"], entry.state["_meta"]
        meta["task_revision"] = (
            meta["rev"]
            if state.get("tasks", []) != entry.state.get("tasks", [])
            else previous.get("task_revision", previous["rev"])
        )
        fragments = document(state, entry.fragments)
        state = fragments.value
        before, after = diff(*(without_events(value) for value in (entry.state, state)))
        paths = write_rows(connection, slug, before, after)
        hierarchy.sync(connection, slug, state)
        kept = len(state["_meta"].get("events", []))
        append_events(connection, slug, events, kept)
        if not events and "events" in state["_meta"]:
            trim_events(connection, slug, kept)
        outbox.write(connection, slug, state["_meta"]["rev"], entry.state, state)
        generation = entry.generation + 1
        refresh.record(connection, slug, generation, paths, CHANGES_KEPT)
        connection.execute(
            UPDATE,
            (state["_meta"]["rev"], generation, encode(summarize(slug, state)), self.domain.now_ms(), slug),
        )
        return Entry(generation, None, state, fragments)

    def _insert(self, connection, slug: str, state: dict, token: str, seeds: dict | None = None) -> None:
        previous = connection.execute(GENERATION, (slug,)).fetchone()
        now = self.domain.now_ms()
        generation = now << GENERATION_SHIFT if previous is None else max(now << GENERATION_SHIFT, previous[0] + 1)
        before = {} if previous is None else self._entry(connection, slug).state
        for table in PER_LEDGER:
            connection.execute(f"DELETE FROM {table} WHERE slug=?", (slug,))
        stored = {**state, "_meta": {**state["_meta"]}}
        events = stored["_meta"].get("events", [])
        if "events" in stored["_meta"]:
            stored["_meta"]["events"] = []
        write_rows(connection, slug, {}, flatten(stored))
        write_events(connection, slug, events)
        write_seeds(connection, slug, seeds or {})
        outbox.write(connection, slug, state["_meta"]["rev"], before, state)
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
        hierarchy.sync(connection, slug, state)
        self._cache.pop(self._key(slug), None)

    def create_document(
        self, slug: str, doc: dict, meta: dict, token: str | None = None, replace: bool = False
    ) -> bool:
        """Store a ledger built from content: its first sync derives priorities, notifications and rev 1."""
        from . import mutation

        with request_metrics.locked(self.domain.LOCK), self.connect() as connection, connection:
            request_metrics.begin(connection, BEGIN_IMMEDIATE)
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
        with request_metrics.locked(self.domain.LOCK), self.connect() as connection, connection:
            request_metrics.begin(connection, BEGIN_IMMEDIATE)
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

    def rebuild(self, slug: str) -> dict:
        self._adopt(slug)
        with request_metrics.locked(self.domain.LOCK), self.connect() as connection, connection:
            request_metrics.begin(connection, BEGIN_IMMEDIATE)
            return hierarchy.rebuild(connection, slug, self._entry(connection, slug).state)

    def nodes(self, slug: str, read: str, node: str | None = None) -> list:
        """Hierarchy rows: `children`, `ancestors`, `subtree` or `dependents` of a node, or of the ledger root."""
        self._adopt(slug)
        with self.connect() as connection, connection:
            connection.execute(BEGIN)
            return hierarchy.read(connection, slug, read, node)

    def events_since(self, slug: str, revision: int) -> list:
        self._adopt(slug)
        with self.connect() as connection:
            connection.execute(BEGIN)
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
        if connection.execute(STORED, (slug,)).fetchone():
            before = self._entry(connection, slug).state
            outbox.write(connection, slug, before["_meta"]["rev"], before, {})
        for table in (*PER_LEDGER, artifact_files.TABLE, media_files.TABLE):
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

    @contextmanager
    def transaction(self):
        """One immediate write transaction under the core lock; entries it wrote are cached once it commits."""
        with request_metrics.locked(self.domain.LOCK), self.connect() as connection:
            with connection:
                request_metrics.begin(connection, BEGIN_IMMEDIATE)
                transaction = Transaction(self, connection)
                yield transaction
        for slug, entry in transaction.written.items():
            self._remember(slug, entry)

    @contextmanager
    def snapshot(self):
        """One read only snapshot of the stored folder, or None when it holds no database."""
        with read_only(self.directory) as connection:
            if connection is None:
                yield None
                return
            connection.execute(BEGIN)
            yield Snapshot(self, connection)

    def held_revisions(self) -> dict:
        """{kind: {slug: [revisions]}} the inbox could not take; kept on disk because Redis is what was down."""
        held = {}
        with self.connect() as connection:
            for kind, slug, revision in connection.execute(HELD):
                held.setdefault(kind, {}).setdefault(slug, []).append(revision)
        return held

    def hold_revision(self, kind: str, slug: str, revision: int) -> None:
        with self.connect() as connection, connection:
            request_metrics.begin(connection, BEGIN_IMMEDIATE)
            connection.execute(HOLD, (kind, slug, revision))

    def release_revisions(self, kind: str, slug: str, since: int, revision: int) -> None:
        with self.connect() as connection, connection:
            request_metrics.begin(connection, BEGIN_IMMEDIATE)
            connection.execute(RELEASE, (kind, slug, since, revision))

    def drop_revisions(self, kind: str, slug: str) -> None:
        with self.connect() as connection, connection:
            request_metrics.begin(connection, BEGIN_IMMEDIATE)
            connection.execute(DROP, (kind, slug))
