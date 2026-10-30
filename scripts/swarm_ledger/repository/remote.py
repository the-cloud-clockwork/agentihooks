import copy
import http.client
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http import HTTPMethod
from pathlib import Path
from types import ModuleType
from urllib.parse import quote, urlencode

from . import artifact_files, media_files
from .entry import Entry
from .fragments import document
from .sqlite import BEGIN, SUMMARY_KEYS, TASK_REVISION_FIELDS, Conflict, Missing, summarize

URL_ENV = "AGENTIHOOKS_SWARM_API_URL"
TOKEN_ENV = "AGENTIHOOKS_SWARM_API_TOKEN"
PREFIX = "/api/v1/store"
ATTEMPTS = 4
TIMEOUT_S = 30.0
AUTHORIZATION = "Authorization"


class Unavailable(OSError):
    pass


class RegistryConflict(ValueError):
    def __init__(self, names: list[str]) -> None:
        self.names = names


class Client:
    def __init__(self, base: str, token: str = "") -> None:
        self.base, self.token = base.removesuffix("/") + PREFIX, token

    def call(self, method: str, path: str, query: dict | None = None, body: object = None) -> tuple[int, object]:
        address = self.base + path + (f"?{urlencode(query, doseq=True)}" if query else "")
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(address, data=data, method=HTTPMethod(method))
        if self.token:
            request.add_header(AUTHORIZATION, f"Bearer {self.token}")
        try:
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                    status, raw = response.status, response.read()
            except urllib.error.HTTPError as answer:
                status, raw = answer.code, answer.read()
            return status, json.loads(raw) if raw else None
        except (OSError, http.client.HTTPException, ValueError) as failure:
            raise Unavailable(str(failure)) from failure

    def expect(self, method: str, path: str, body: object = None) -> object:
        status, answer = self.call(method, path, body=body)
        if status != 200:
            raise Unavailable(f"{status} {answer}")
        return answer

    def get(self, slug: str, path: str, query: dict | None = None) -> object:
        status, body = self.call("GET", path, query)
        if status == 404:
            raise Missing(slug)
        if status != 200:
            raise Unavailable(f"{status} {body}")
        return body


def ledger(slug: str, part: str = "") -> str:
    return f"/ledgers/{quote(slug)}" + (f"/{part}" if part else "")


def stored_file(table: str, slug: str, item: str = "") -> str:
    return f"/files/{table}/{quote(slug)}" + (f"/{quote(item)}" if item else "")


def changes(before: dict, after: dict) -> tuple[dict, list]:
    changed = {key: value for key, value in after.items() if key not in before or before[key] != value}
    return changed, [key for key in before if key not in after]


class Rows:
    """The rows the API answered for one statement, read the way a sqlite cursor is read."""

    def __init__(self, rows: list) -> None:
        self.rows = rows

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list:
        return self.rows


class Session:
    """A registry write against the API: reads remember what they saw, saves and purges commit together on a clean
    exit, refused with Conflict when another writer changed a registry this session read."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.seen: dict[str, dict] = {}
        self.saved: dict[str, dict] = {}
        self.purged: list[str] = []

    def execute(self, statement: str, parameters: tuple = ()) -> Rows:
        if statement.startswith(BEGIN):
            return Rows([])
        if statement not in FILE_STATEMENTS:
            raise Unavailable(statement)
        answer, table = FILE_STATEMENTS[statement]
        return answer(self, table, *parameters)

    def file_save(self, table: str, slug: str, item: str, data: str, at: int | None = None) -> Rows:
        self.client.expect("PUT", stored_file(table, slug, item), body=dict(data=data, at=at))
        return Rows([])

    def file_load(self, table: str, slug: str, item: str) -> Rows:
        status, body = self.client.call("GET", stored_file(table, slug, item))
        if status == 404:
            return Rows([])
        if status != 200:
            raise Unavailable(f"{status} {body}")
        return Rows([(body["data"],)])

    def file_list(self, table: str, slug: str) -> Rows:
        return Rows(list(self.client.expect("GET", stored_file(table, slug)).items()))

    def file_drop(self, table: str, slug: str, item: str) -> Rows:
        self.client.expect("DELETE", stored_file(table, slug, item))
        return Rows([])

    def registry(self, name: str) -> dict:
        if name not in self.seen:
            self.seen[name] = self.client.expect("GET", f"/registries/{quote(name)}")
        return copy.deepcopy(self.seen[name])

    def commit(self) -> None:
        if not self.saved and not self.purged:
            return
        body = dict(expected=self.seen, set=self.saved, purge=self.purged)
        status, answer = self.client.call("POST", "/registries", body=body)
        if status == 409:
            raise RegistryConflict(answer["registries"])
        if status != 200:
            raise Unavailable(f"{status} {answer}")
        self.seen.update(self.saved)
        self.saved, self.purged = {}, []

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, kind: type | None, value: BaseException | None, traceback: object) -> bool:
        if kind is None:
            self.commit()
        return False


FILE_STATEMENTS = {
    artifact_files.SAVE: (Session.file_save, artifact_files.TABLE),
    artifact_files.LOAD: (Session.file_load, artifact_files.TABLE),
    artifact_files.IDS: (Session.file_list, artifact_files.TABLE),
    artifact_files.DROP: (Session.file_drop, artifact_files.TABLE),
    media_files.SAVE: (Session.file_save, media_files.TABLE),
    media_files.LOAD: (Session.file_load, media_files.TABLE),
    media_files.AGES: (Session.file_list, media_files.TABLE),
    media_files.DROP: (Session.file_drop, media_files.TABLE),
}


class RemoteLedgerRepository:
    adopts = False
    remote = True

    def __init__(self, client: Client, domain: ModuleType | None = None) -> None:
        self.client = client
        self._domain = domain
        self._cache: dict[str, Entry] = {}
        self._guard = threading.Lock()

    @property
    def domain(self) -> ModuleType:
        if self._domain is None:
            import ledger_core

            self._domain = ledger_core
        return self._domain

    @property
    def directory(self) -> Path:
        return self.domain.LEDGER_DIR

    def bound(self, domain: ModuleType) -> "RemoteLedgerRepository":
        if domain is self.domain:
            return self
        twin = RemoteLedgerRepository(self.client, domain)
        twin._cache, twin._guard = self._cache, self._guard
        return twin

    def _remember(self, slug: str, entry: Entry) -> Entry:
        with self._guard:
            held = self._cache.get(slug)
            if held is None or held.generation < entry.generation:
                self._cache[slug] = entry
            return self._cache[slug]

    def current(self, slug: str) -> Entry:
        with self._guard:
            held = self._cache.get(slug)
        query = None if held is None else {"read": held.generation}
        status, body = self.client.call("GET", ledger(slug), query)
        if status == 304 and held is not None:
            return held
        if status == 404:
            with self._guard:
                self._cache.pop(slug, None)
            raise Missing(slug)
        if status != 200:
            raise Unavailable(f"{status} {body}")
        fragments = document(body["state"])
        return self._remember(slug, Entry(body["generation"], None, fragments.value, fragments))

    def get_document(self, slug: str) -> dict:
        return self.current(slug).mutable()

    def read(self, slug: str, *keys: str) -> dict:
        return self.client.get(slug, ledger(slug, "read"), {"read": list(keys)})

    def exists(self, slug: str) -> bool:
        try:
            self.token(slug)
        except Missing:
            return False
        return True

    def token(self, slug: str) -> str | None:
        return self.client.get(slug, ledger(slug, "token"))["token"]

    def events_since(self, slug: str, revision: int) -> list:
        return self.client.get(slug, ledger(slug, "events"), {"events": revision})

    def nodes(self, slug: str, read: str, node: str | None = None) -> list:
        query = {"read": read} if node is None else {"read": read, "nodes": node}
        return self.client.get(slug, ledger(slug, "nodes"), query)

    def summaries(self, connection: Session | None = None) -> list:
        return self.client.expect("GET", "/ledgers")

    def list_summaries(self) -> list:
        return [{key: summary[key] for key in SUMMARY_KEYS} for summary in self.summaries()]

    def export_since(
        self, slug: str, since: int, view: Callable[[object], str | bytes], patch: Callable[..., object]
    ) -> tuple[int, str | bytes]:
        entry = self.current(slug)
        if view not in entry.views:
            entry.views[view] = view(entry.fragments)
        return entry.generation, entry.views[view]

    def task_rows(self, slug: str, since: tuple | None = None) -> tuple:
        entry = self.current(slug)
        if since is not None and since[0] == entry.generation:
            return since
        meta = {name: entry.state["_meta"][name] for name in TASK_REVISION_FIELDS if name in entry.state["_meta"]}
        return entry.generation, meta, entry.state.get("tasks") or []

    def commit(self, slug: str, revision: int, before: dict, state: dict, events: list) -> Entry:
        changed, dropped = changes(before, state)
        body = dict(revision=revision, changed=changed, dropped=dropped, events=events, summary=summarize(slug, state))
        status, answer = self.client.call("POST", ledger(slug, "commit"), body=body)
        if status == 409:
            raise Conflict(slug, answer["revision"])
        if status == 404:
            raise Missing(slug)
        if status != 200:
            raise Unavailable(f"{status} {answer}")
        fragments = document(state)
        return self._remember(slug, Entry(answer["generation"], None, fragments.value, fragments))

    def apply_ops(
        self,
        slug: str,
        changes: list | None = None,
        ops: list | None = None,
        gate: object = None,
        *,
        copy_state: bool = True,
    ) -> tuple[dict, list]:
        for _ in range(ATTEMPTS - 1):
            try:
                return self._ruled(slug, changes, ops, gate, copy_state)
            except Conflict:
                continue
        return self._ruled(slug, changes, ops, gate, copy_state)

    def _ruled(
        self, slug: str, changes: list | None, ops: list | None, gate: object, copy_state: bool
    ) -> tuple[dict, list]:
        from . import mutation

        entry = self.current(slug)
        state = entry.mutable()
        revision = state["_meta"]["rev"]
        rejected, ctx = mutation.apply(slug, state, self.domain, copy.deepcopy(changes), copy.deepcopy(ops), gate)
        written = self.commit(slug, revision, entry.state, state, ctx.events if ctx.changed else [])
        artifact_files.drop(slug, ctx.unstored)
        media_files.drop(slug, ctx.unstored)
        return (written.mutable() if copy_state else written.state), rejected

    def generations(self) -> dict:
        with self._guard:
            return {slug: entry.generation for slug, entry in self._cache.items()}

    def carry(self, since: dict) -> dict:
        with self._guard:
            held = dict(self._cache)
        return {
            slug: [entry.generation, entry.text] for slug, entry in held.items() if since.get(slug) != entry.generation
        }

    def adopt_carried(self, carried: dict) -> None:
        for slug, (generation, text) in carried.items():
            fragments = document(json.loads(text))
            self._remember(slug, Entry(generation, None, fragments.value, fragments))

    def held_revisions(self) -> dict:
        return self.client.expect("GET", "/held")

    def hold_revision(self, kind: str, slug: str, revision: int) -> None:
        self.client.expect("POST", "/held", body=dict(action="hold", kind=kind, slug=slug, revision=revision))

    def release_revisions(self, kind: str, slug: str, since: int, revision: int) -> None:
        body = dict(action="release", kind=kind, slug=slug, start=since, revision=revision)
        self.client.expect("POST", "/held", body=body)

    def drop_revisions(self, kind: str, slug: str) -> None:
        self.client.expect("POST", "/held", body=dict(action="drop", kind=kind, slug=slug))

    @contextmanager
    def connect(self) -> Iterator[Session]:
        with Session(self.client) as session:
            yield session

    def registry(self, name: str, connection: Session) -> dict:
        return connection.registry(name)

    def save_registry(self, connection: Session, name: str, entries: dict) -> None:
        connection.saved[name] = entries

    def purge(self, slug: str, connection: Session) -> None:
        connection.purged.append(slug)
        with self._guard:
            self._cache.pop(slug, None)
