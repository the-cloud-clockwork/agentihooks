import copy
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .events import read_events, write_events
from .rows import TABLES, assemble, encode, flatten, read_rows, write_rows
from .seeds import read_seeds, sync_values, write_seeds

SCHEMA = """
CREATE TABLE IF NOT EXISTS registry (slug TEXT, path TEXT, value TEXT, PRIMARY KEY(slug,path));
CREATE TABLE IF NOT EXISTS ledgers (slug TEXT PRIMARY KEY, revision INTEGER NOT NULL, deleted_at INTEGER, restored_at INTEGER, source_signature TEXT);
CREATE TABLE IF NOT EXISTS revisions (slug TEXT, revision TEXT, position INTEGER, PRIMARY KEY(slug,revision));
CREATE TABLE IF NOT EXISTS seed_base (slug TEXT, path TEXT, value TEXT, PRIMARY KEY(slug,path));
CREATE TABLE IF NOT EXISTS seed_deltas (slug TEXT, revision TEXT, path TEXT, value TEXT, PRIMARY KEY(slug,revision,path));
CREATE TABLE IF NOT EXISTS events (slug TEXT, key TEXT, revision INTEGER, position INTEGER, value TEXT, PRIMARY KEY(slug,key));
CREATE INDEX IF NOT EXISTS events_revision ON events(slug,revision);
"""


class SQLiteLedgerRepository:
    def __init__(self, path: Path, files=None):
        self.path = Path(path)
        self.files = files
        self.trace = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            for table in TABLES:
                connection.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} (slug TEXT, path TEXT, parent TEXT, key TEXT, position INTEGER, kind TEXT, value TEXT, PRIMARY KEY(slug,path))"
                )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA synchronous=FULL")
        connection.set_trace_callback(self.trace)
        try:
            yield connection
        finally:
            connection.close()

    def _document(self, connection, slug: str) -> dict:
        rows = read_rows(connection, slug)
        if not rows:
            raise KeyError(slug)
        state = assemble(rows)
        state["_meta"]["seeds"] = read_seeds(connection, slug)
        if "events" in state["_meta"]:
            state["_meta"]["events"] = read_events(connection, slug)
        return state

    def get_document(self, slug: str, reconcile: bool = True) -> dict:
        if reconcile and self.files is not None:
            self.files.get_document(slug)
        with self.connect() as connection, connection:
            connection.execute("BEGIN")
            return self._document(connection, slug)

    def import_document(
        self,
        slug: str,
        state: dict,
        deleted_at: int | None = None,
        restored_at: int | None = None,
        source_signature: str | None = None,
        registries: dict | None = None,
    ) -> None:
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if registries is not None:
                self._registries(connection, registries)
            self._write_document(connection, slug, state, (deleted_at, restored_at, source_signature))

    def _write_document(self, connection, slug: str, state: dict, lifecycle: tuple) -> None:
        before = connection.execute(
            "SELECT revision,deleted_at,restored_at,source_signature FROM ledgers WHERE slug=?", (slug,)
        ).fetchone()
        target = (state["_meta"]["rev"], *lifecycle)
        if lifecycle[2] is not None and before == target:
            return
        document = copy.deepcopy(state)
        meta = document["_meta"]
        seeds, events = meta.pop("seeds"), meta.get("events", [])
        if "events" in meta:
            meta["events"] = []
        if before != target:
            connection.execute(
                "INSERT INTO ledgers VALUES (?, ?, ?, ?, ?) ON CONFLICT(slug) DO UPDATE SET revision=excluded.revision,deleted_at=excluded.deleted_at,restored_at=excluded.restored_at,source_signature=excluded.source_signature",
                (slug, *target),
            )
        write_rows(connection, slug, read_rows(connection, slug), flatten(document))
        write_seeds(connection, slug, seeds)
        write_events(connection, slug, events)
        self.verify(connection, slug, state)

    def _registries(self, connection, registries: dict) -> None:
        for name, entries in registries.items():
            sync_values(connection, "registry", name, {key: encode(value) for key, value in entries.items()})
            actual = {
                key: json.loads(value)
                for key, value in connection.execute("SELECT path,value FROM registry WHERE slug=?", (name,))
            }
            if actual != entries:
                raise ValueError(f"SQLite shadow registry differs for {name}")

    def apply_lifecycle(self, directory: Path, registries: dict, removed: list | None = None) -> None:
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._registries(connection, registries)
            existing = {slug for (slug,) in connection.execute("SELECT slug FROM ledgers")}
            present = {path.stem for pattern in ("*.html", "*.json") for path in directory.glob(pattern)}
            removed = set(removed or []) | (existing - present)
            for slug in removed:
                for table in (*TABLES, "ledgers", "revisions", "seed_base", "seed_deltas", "events"):
                    connection.execute(f"DELETE FROM {table} WHERE slug=?", (slug,))
            known = {
                slug: (deleted, restored)
                for slug, deleted, restored in connection.execute("SELECT slug,deleted_at,restored_at FROM ledgers")
            }
            paths = {path.stem: path for path in directory.glob("*.json") if path.with_suffix(".html").exists()}
            candidates = known.keys() | registries["bin"].keys() | registries["restored"].keys()
            for slug in candidates - set(removed or []):
                target = tuple(
                    registries[name].get(slug) if isinstance(registries[name].get(slug), int) else None
                    for name in ("bin", "restored")
                )
                if slug in known:
                    if known[slug] != target:
                        connection.execute(
                            "UPDATE ledgers SET deleted_at=?,restored_at=? WHERE slug=?", (*target, slug)
                        )
                else:
                    path = paths.get(slug)
                    if path is not None:
                        state = json.loads(path.read_text(encoding="utf-8"))
                        self._write_document(connection, slug, state, (*target, None))

    def verify(self, connection, slug: str, state: dict) -> None:
        if self._document(connection, slug) != state:
            raise ValueError(f"SQLite shadow document differs for {slug}")

    def events_since(self, slug: str, revision: int) -> list:
        with self.connect() as connection:
            if connection.execute("SELECT 1 FROM ledgers WHERE slug=?", (slug,)).fetchone() is None:
                raise KeyError(slug)
            return read_events(connection, slug, revision)

    def get_seed(self, slug: str, revision: str) -> dict:
        return self.get_document(slug, reconcile=False)["_meta"]["seeds"][revision]

    def import_registry(self, name: str, entries: dict) -> None:
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            sync_values(connection, "registry", name, {key: encode(value) for key, value in entries.items()})

    def registry(self, name: str) -> dict:
        with self.connect() as connection:
            return {
                key: json.loads(value)
                for key, value in connection.execute("SELECT path,value FROM registry WHERE slug=?", (name,))
            }

    def lifecycle(self, slug: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT deleted_at,restored_at FROM ledgers WHERE slug=?", (slug,)).fetchone()
        if row is None:
            raise KeyError(slug)
        return dict(zip(("deleted_at", "restored_at"), row, strict=True))

    def _files(self):
        if self.files is None:
            raise RuntimeError("SQLite is a shadow; mutations require the authoritative file repository")
        return self.files

    def apply_ops(
        self, slug: str, changes: list | None = None, ops: list | None = None, gate=None
    ) -> tuple[dict, list]:
        return self._files().apply_ops(slug, changes=changes, ops=ops, gate=gate)

    def list_summaries(self) -> list:
        return self._files().list_summaries()

    def create(self, slug: str, content: dict, size: str = "small") -> bool:
        return self._files().create(slug, content, size)

    def delete(self, slug: str, now: int | None = None) -> None:
        self._files().delete(slug, now)

    def restore(self, slug: str, now: int | None = None) -> bool:
        return self._files().restore(slug, now)
