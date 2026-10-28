"""The hosted session memory service: authenticated HTTP over the session search and session tool calls.

A caller's grant comes only from the server's token file, keyed by the SHA-256 of each bearer token, never from
the request; a missing or unknown token is refused before the catalog is read. Run it with
``python -m hooks.memory.service``; ``AGENTIHOOKS_SESSION_CATALOG`` names a SQLite file or a Postgres URL.
"""

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
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
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
        length = int(request.headers.get("Content-Length") or 0)
        if length not in range(1, MAX_BODY + 1):
            return None
        body = json.loads(request.rfile.read(length))
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def post(tools: records.SessionTools, grants: dict[str, Grant], request: BaseHTTPRequestHandler) -> tuple:
    if request.path != SEARCH_PATH and not request.path.startswith(TOOLS_PREFIX):
        return 404, _refusal("not_found", NOT_FOUND)
    grant = authenticate(grants, request.headers.get("Authorization"))
    if grant is None:
        return 401, _refusal("unauthenticated", UNAUTHENTICATED), {"WWW-Authenticate": "Bearer"}
    body = read_body(request)
    if body is None:
        return 400, _refusal("invalid_request", BAD_BODY)
    if request.path == SEARCH_PATH:
        return 200, search_api.search(tools.searcher, grant, body)
    return 200, records.call(tools, request.path.removeprefix(TOOLS_PREFIX), grant, body, time.time())


def handler(tools: records.SessionTools, grants: dict[str, Grant]) -> type[BaseHTTPRequestHandler]:
    class Sessions(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != HEALTH_PATH:
                return self._answer(404, _refusal("not_found", NOT_FOUND))
            try:
                tools.catalog.db.execute("SELECT 1").fetchone()
            except sqlite3.OperationalError:
                return self._answer(503, _refusal("dependency_unavailable", CATALOG_DOWN))
            return self._answer(200, {"state": "ready"})

        def do_POST(self):
            self._answer(*post(tools, grants, self))

        def _answer(self, status: int, body: dict, headers: dict | None = None):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    return Sessions


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
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
