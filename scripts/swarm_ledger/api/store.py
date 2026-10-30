import argparse
import base64
import hmac
import json
import os
import sys
from contextlib import nullcontext
from enum import StrEnum, auto
from http import HTTPMethod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from scripts.swarm_ledger import request_metrics, storage
from scripts.swarm_ledger.repository import LedgerRepository, artifact_files, media_files, plain
from scripts.swarm_ledger.storage import BEGIN_IMMEDIATE, Conflict, Missing

PREFIX = "/api/v1/store"
TOKEN_ENV = "AGENTIHOOKS_SWARM_API_TOKEN"
PORT_ENV = "AGENTIHOOKS_SWARM_API_PORT"
DEFAULT_PORT = 8766
MAX_BODY = 64 << 20
FILES = {artifact_files.TABLE: artifact_files, media_files.TABLE: media_files}
WITH_BODY = {HTTPMethod.POST, HTTPMethod.PUT}


class Code(StrEnum):
    NOT_FOUND = auto()
    LEDGER_NOT_FOUND = auto()
    REVISION_CONFLICT = auto()
    UNKNOWN_ACTION = auto()
    UNAUTHORIZED = auto()
    BAD_REQUEST = auto()
    STORE_FAILED = auto()


class Part(StrEnum):
    LEDGERS = auto()
    HELD = auto()
    FILES = auto()
    REGISTRIES = auto()
    COMMIT = auto()
    READ = auto()
    TOKEN = auto()
    EVENTS = auto()
    NODES = auto()


class Action(StrEnum):
    HOLD = auto()
    RELEASE = auto()
    DROP = auto()


def repository() -> LedgerRepository:
    return storage.owned(plain)


class Answer(Exception):
    def __init__(self, status: int, body: object = None) -> None:
        super().__init__(status)
        self.status, self.body = status, body


def refused(status: int, code: Code, **details: object) -> Answer:
    return Answer(status, dict(code=code.value, **details))


def first(query: dict, key: str, default: str | None) -> str | None:
    return query.get(key, [default])[0]


def reading(stored: LedgerRepository, slug: str, part: str | None, query: dict) -> object:
    if part is None:
        entry = stored.current(slug)
        if first(query, Part.READ, None) == str(entry.generation):
            raise Answer(304)
        return dict(generation=entry.generation, state=entry.state)
    if part == Part.READ:
        return stored.read(slug, *query.get(Part.READ, []))
    if part == Part.TOKEN:
        token = stored.token(slug)
        if token is None:
            raise Missing(slug)
        return dict(token=token)
    if part == Part.EVENTS:
        return stored.events_since(slug, int(first(query, Part.EVENTS, "0")))
    if part == Part.NODES:
        return stored.nodes(slug, first(query, Part.READ, "children"), first(query, Part.NODES, None))
    raise refused(404, Code.NOT_FOUND)


def commit_shape(body: dict) -> None:
    shapes = dict(revision=int, changed=dict, dropped=list, events=list, summary=dict)
    wrong = sorted(name for name, kind in shapes.items() if type(body.get(name)) is not kind)
    if wrong or "_meta" in body["dropped"] or not isinstance(body["changed"].get("_meta", {}), dict):
        raise refused(400, Code.BAD_REQUEST, fields=wrong or ["_meta"])


def committing(stored: LedgerRepository, slug: str, body: dict) -> dict:
    commit_shape(body)
    try:
        written = stored.commit(
            slug, body["revision"], body["changed"], body["dropped"], body["events"], body["summary"]
        )
    except Conflict as conflict:
        raise refused(409, Code.REVISION_CONFLICT, revision=conflict.revision) from conflict
    return dict(generation=written.generation, revision=written.state["_meta"]["rev"])


def holding(stored: LedgerRepository, body: dict) -> dict:
    action = body.get("action")
    if action == Action.HOLD:
        stored.hold_revision(body["kind"], body["slug"], body["revision"])
    elif action == Action.RELEASE:
        stored.release_revisions(body["kind"], body["slug"], body["start"], body["revision"])
    elif action == Action.DROP:
        stored.drop_revisions(body["kind"], body["slug"])
    else:
        raise refused(400, Code.UNKNOWN_ACTION)
    return dict(ok=True)


def file_read(stored: LedgerRepository, table: str, slug: str, item: str | None) -> object:
    with stored.connect() as connection:
        if item is None:
            return dict(connection.execute(FILES[table].LIST, (slug,)).fetchall())
        row = connection.execute(FILES[table].LOAD, (slug, item)).fetchone()
    if row is None:
        raise refused(404, Code.NOT_FOUND)
    return dict(data=row[0])


def file_write(stored: LedgerRepository, table: str, slug: str, item: str, body: dict) -> dict:
    data = body["data"]
    base64.b64decode(data, validate=True)
    arguments = (slug, item, data, body["at"]) if table == media_files.TABLE else (slug, item, data)
    with stored.connect() as connection, connection:
        connection.execute(FILES[table].SAVE, arguments)
    return dict(ok=True)


def file_drop(stored: LedgerRepository, table: str, slug: str, item: str) -> dict:
    with stored.connect() as connection, connection:
        connection.execute(FILES[table].DROP, (slug, item))
    return dict(ok=True)


def ledger_route(stored: LedgerRepository, method: str, parts: list, query: dict, body: dict | None) -> object:
    slug, part = parts[0], (parts[1] if len(parts) > 1 else None)
    if method == HTTPMethod.POST and part == Part.COMMIT:
        return committing(stored, slug, body or {})
    if method == HTTPMethod.GET:
        return reading(stored, slug, part, query)
    raise refused(404, Code.NOT_FOUND)


def file_route(stored: LedgerRepository, method: str, parts: list, body: dict | None) -> object:
    table, slug, item = parts[0], parts[1], (parts[2] if len(parts) > 2 else None)
    if table not in FILES:
        raise refused(404, Code.NOT_FOUND)
    if method == HTTPMethod.GET:
        return file_read(stored, table, slug, item)
    if method == HTTPMethod.PUT and item:
        return file_write(stored, table, slug, item, body or {})
    if method == HTTPMethod.DELETE and item:
        return file_drop(stored, table, slug, item)
    raise refused(404, Code.NOT_FOUND)


def registry_read(stored: LedgerRepository, name: str) -> dict:
    with stored.connect() as connection:
        return stored.registry(name, connection)


def registry_commit(stored: LedgerRepository, body: dict) -> dict:
    unread = sorted(set(body["set"]) - set(body["expected"]))
    if unread:
        raise refused(400, Code.BAD_REQUEST, registries=unread)
    with request_metrics.locked(stored.domain.LOCK), stored.connect() as connection, connection:
        request_metrics.begin(connection, BEGIN_IMMEDIATE)
        moved = [name for name, held in body["expected"].items() if stored.registry(name, connection) != held]
        if moved:
            raise refused(409, Code.REVISION_CONFLICT, registries=moved)
        for slug in body["purge"]:
            stored.purge(slug, connection)
        for name, entries in body["set"].items():
            stored.save_registry(connection, name, entries)
    return dict(ok=True)


def route(stored: LedgerRepository, method: str, path: str, query: dict, body: dict | None) -> object:
    parts = [unquote(part) for part in path.split("/") if part]
    head, rest = (parts[0], parts[1:]) if parts else ("", [])
    if head == Part.REGISTRIES and len(rest) == 1 and method == HTTPMethod.GET:
        return registry_read(stored, rest[0])
    if head == Part.REGISTRIES and not rest and method == HTTPMethod.POST:
        return registry_commit(stored, body or {})
    if head == Part.LEDGERS and not rest and method == HTTPMethod.GET:
        return stored.summaries()
    if head == Part.HELD and not rest:
        return stored.held_revisions() if method == HTTPMethod.GET else holding(stored, body or {})
    if head == Part.LEDGERS and 1 <= len(rest) <= 2:
        return ledger_route(stored, method, rest, query, body)
    if head == Part.FILES and 2 <= len(rest) <= 3:
        return file_route(stored, method, rest, body)
    raise refused(404, Code.NOT_FOUND)


class Handler(BaseHTTPRequestHandler):
    stored: LedgerRepository
    token: str = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_GET(self) -> None:
        self.serve(HTTPMethod.GET)

    def do_POST(self) -> None:
        self.serve(HTTPMethod.POST)

    def do_PUT(self) -> None:
        self.serve(HTTPMethod.PUT)

    def do_DELETE(self) -> None:
        self.serve(HTTPMethod.DELETE)

    def serve(self, method: HTTPMethod) -> None:
        parts = urlsplit(self.path)
        try:
            if parts.path == "/healthz":
                raise Answer(200, dict(ok=True))
            if not parts.path.startswith(PREFIX + "/"):
                raise refused(404, Code.NOT_FOUND)
            if not self.allowed():
                raise refused(401, Code.UNAUTHORIZED)
            body = self.body() if method in WITH_BODY else None
            answer = Answer(200, route(self.stored, method, parts.path[len(PREFIX) :], parse_qs(parts.query), body))
        except Answer as given:
            answer = given
        except Missing:
            answer = refused(404, Code.LEDGER_NOT_FOUND)
        except (KeyError, TypeError, ValueError) as failure:
            answer = refused(400, Code.BAD_REQUEST, detail=str(failure))
        except Exception as failure:  # noqa: BLE001
            answer = refused(500, Code.STORE_FAILED, detail=type(failure).__name__)
        if answer.status >= 400:
            self.close_connection = True
        self.send(answer)

    def allowed(self) -> bool:
        if not self.token:
            return True
        given = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return hmac.compare_digest(given.encode(), self.token.encode())

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= MAX_BODY:
            raise ValueError(length)
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise TypeError(type(value).__name__)
        return value

    def send(self, answer: Answer) -> None:
        data = b"" if answer.status == 304 else json.dumps(answer.body, separators=(",", ":")).encode()
        self.send_response(answer.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def server(stored: LedgerRepository, host: str, port: int, token: str) -> ThreadingHTTPServer:
    handler = type("Bound", (Handler,), dict(stored=stored, token=token))
    listening = ThreadingHTTPServer((host, port), handler)
    listening.daemon_threads = True
    return listening


def relaying(stored: LedgerRepository, relay: bool) -> object:
    if not relay:
        return nullcontext()
    from scripts.swarm_ledger import outbox_host

    return outbox_host.running(stored)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks-swarm-api")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get(PORT_ENV, DEFAULT_PORT)))
    parser.add_argument("--no-relay", action="store_true")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--folder", action="store_true")
    options = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "")
    if not token and not options.open:
        parser.error(f"{TOKEN_ENV} is unset; pass --open to serve without a token")
    if not storage.shared() and not options.folder:
        parser.error("no shared database URL; pass --folder to serve the ledger folder")
    stored = repository()
    listening = server(stored, options.host, options.port, token)
    print(f"swarm api on {options.host}:{listening.server_address[1]}", flush=True)
    with relaying(stored, not options.no_relay):
        try:
            listening.serve_forever(0.2)
        finally:
            listening.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
