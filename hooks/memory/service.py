"""The hosted session memory service: authenticated HTTP over the session search and session tool calls.

A caller's grant comes only from the server's token file, keyed by the SHA-256 of each bearer token, never from
the request; a missing or unknown token is refused before the catalog is read. Run it with
``python -m hooks.memory.service``; ``AGENTIHOOKS_SESSION_CATALOG`` names a SQLite file or a Postgres URL.
"""

import contextlib
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from hooks.memory import archive
from hooks.memory.api import ingest as ingest_api
from hooks.memory.api import records
from hooks.memory.api import search as search_api
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant
from hooks.memory.catalog import Catalog

CATALOG_ENV = "AGENTIHOOKS_SESSION_CATALOG"
OBJECTS_ENV = "AGENTIHOOKS_SESSION_OBJECTS"
TOKENS_ENV = "AGENTIHOOKS_SESSION_TOKENS"
HOST_ENV = "AGENTIHOOKS_SESSION_HOST"
PORT_ENV = "AGENTIHOOKS_SESSION_PORT"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8790
MAX_BODY = 1 << 20
LENGTH_HEADER = "Content-Length"
AUTH_HEADER = "Authorization"
PROBE = "SELECT 1"
INGEST_PATH = "/v2/sessions/ingest"
SEARCH_PATH = "/v2/sessions/search"
TOOLS_PREFIX = "/v2/sessions/tools/"
HEALTH_PATH = "/healthz"
UNAUTHENTICATED = "a valid bearer token is required"
NOT_FOUND = "no session route has this path"
BAD_BODY = "the body must be a JSON object of at most 1 MiB"
CATALOG_DOWN = "the session catalog did not answer"
REQUIRED = (CATALOG_ENV, OBJECTS_ENV, TOKENS_ENV)
MISSING_CONFIG = "AGENTIHOOKS_SESSION_CATALOG, AGENTIHOOKS_SESSION_OBJECTS and AGENTIHOOKS_SESSION_TOKENS must be set"


def load_grants(path: str | Path) -> dict[str, Grant]:
    entries = json.loads(Path(path).read_bytes())
    return {
        token_digest: Grant(entry["principal"], entry["corpus"], frozenset(entry["projects"]))
        for token_digest, entry in entries.items()
    }


def authenticate(grants: dict[str, Grant], header: str | None) -> Grant | None:
    scheme, _, token = (header or "").partition(" ")
    if scheme != "Bearer" or not token:
        return None
    offered = hashlib.sha256(token.encode()).hexdigest()
    found = None
    for token_digest, grant in grants.items():
        if hmac.compare_digest(offered, token_digest):
            found = grant
    return found


def _refusal(error_class: str, detail: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "rejected",
        "error": {"class": error_class, "retry": "never", "detail": detail},
    }


def read_body(request: BaseHTTPRequestHandler) -> dict | None:
    try:
        length = int(request.headers.get(LENGTH_HEADER) or 0)
        if length not in range(2, MAX_BODY + 1):
            return None
        body = json.loads(request.rfile.read(length))
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def post(tools: records.SessionTools, grants: dict[str, Grant], request: BaseHTTPRequestHandler) -> tuple:
    if request.path not in (SEARCH_PATH, INGEST_PATH) and not request.path.startswith(TOOLS_PREFIX):
        return 404, _refusal("not_found", NOT_FOUND)
    grant = authenticate(grants, request.headers.get(AUTH_HEADER))
    if grant is None:
        return 401, _refusal("unauthenticated", UNAUTHENTICATED), {"WWW-Authenticate": "Bearer"}
    body = read_body(request)
    if body is None:
        return 400, _refusal("invalid_request", BAD_BODY)
    if request.path == INGEST_PATH:
        return 200, ingest_api.ingest(tools.catalog, grant, body)
    if request.path == SEARCH_PATH:
        return 200, search_api.search(tools.searcher, grant, body)
    return 200, records.call(tools, request.path.removeprefix(TOOLS_PREFIX), grant, body, time.time())


def answer(request: BaseHTTPRequestHandler, status: int, body: dict, headers: dict | None = None) -> None:
    payload = json.dumps(body).encode()
    request.send_response(status)
    request.send_header("Content-Type", "application/json")
    request.send_header(LENGTH_HEADER, str(len(payload)))
    for name, value in (headers or {}).items():
        request.send_header(name, value)
    request.end_headers()
    request.wfile.write(payload)


def get(tools: records.SessionTools, request: BaseHTTPRequestHandler) -> tuple:
    if request.path != HEALTH_PATH:
        return 404, _refusal("not_found", NOT_FOUND)
    try:
        tools.catalog.db.execute(PROBE).fetchone()
    except sqlite3.OperationalError:
        return 503, _refusal("dependency_unavailable", CATALOG_DOWN)
    return 200, {"state": "ready"}


def quiet(*_: object) -> None:
    return None


def handler(tools: records.SessionTools, grants: dict[str, Grant]) -> type[BaseHTTPRequestHandler]:
    routes = {
        "do_GET": lambda request: answer(request, *get(tools, request)),
        "do_POST": lambda request: answer(request, *post(tools, grants, request)),
        "log_message": quiet,
    }
    return type("Sessions", (BaseHTTPRequestHandler,), routes)


def build(environ: dict) -> HTTPServer:
    if not all(environ.get(name) for name in REQUIRED):
        raise SystemExit(MISSING_CONFIG)
    catalog, store = Catalog(environ[CATALOG_ENV]), archive.ObjectStore(environ[OBJECTS_ENV])
    tools = records.SessionTools(catalog, store)
    address = (environ.get(HOST_ENV) or DEFAULT_HOST, int(environ.get(PORT_ENV) or DEFAULT_PORT))
    return HTTPServer(address, handler(tools, load_grants(environ[TOKENS_ENV])))


def main() -> int:
    server = build(dict(os.environ))
    print(f"session memory service listening on {server.server_address[0]}:{server.server_port}", file=sys.stderr)
    try:
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
