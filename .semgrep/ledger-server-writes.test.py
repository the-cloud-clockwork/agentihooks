from http.server import BaseHTTPRequestHandler
from typing import Any

from scripts.swarm_ledger.api import client as api_client
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.swarm_ledger.api.client import ResourceClient as Client
from scripts.tick.adapters import database_ledger


class Handler(BaseHTTPRequestHandler):
    # ok: ledger-server-reads-only
    def do_GET(self) -> None:
        self.send_response(200)

    # ok: ledger-server-reads-only
    def do_OPTIONS(self) -> None:
        self.send_response(204)

    # ruleid: ledger-server-reads-only
    def do_POST(self) -> None:
        self.send_response(200)

    # ruleid: ledger-server-reads-only
    def do_PUT(self) -> None:
        self.send_response(200)

    # ruleid: ledger-server-reads-only
    def do_PATCH(self) -> None:
        self.send_response(200)

    # ruleid: ledger-server-reads-only
    def do_DELETE(self) -> None:
        self.send_response(200)


class Mounted(Handler):
    # ruleid: ledger-server-reads-only
    do_POST = Handler.do_GET


class Annotated(Handler):
    # ruleid: ledger-server-reads-only
    do_PUT: Any = Handler.do_GET


# ruleid: ledger-server-reads-only
Handler.do_PATCH = Handler.do_GET
# ruleid: ledger-server-reads-only
setattr(Handler, "do_DELETE", Handler.do_GET)
# ok: ledger-server-reads-only
setattr(Handler, "do_HEAD", Handler.do_GET)


def mount(verb: str) -> None:
    # ruleid: ledger-server-reads-only
    setattr(Handler, verb, Handler.do_GET)
    # ruleid: ledger-server-reads-only
    setattr(Handler, f"do_{verb}", Handler.do_GET)


def over_http(base: str, headers: dict[str, str]) -> ResourceClient:
    # ruleid: ledger-client-in-process
    return ResourceClient(base, headers, 10)


def task_update_over_http(base: str, slug: str) -> dict[str, Any]:
    # ruleid: ledger-client-in-process
    return ResourceClient(base, {}, 10).mutate(slug, [{"op": "task_update", "item": "tasks/t1", "fields": {}}])


def over_http_module(base: str, token: str) -> ResourceClient:
    # ruleid: ledger-client-in-process
    return api_client.ResourceClient(base, {"X-Ledger-Token": token}, 10)


def over_http_alias(base: str) -> ResourceClient:
    # ruleid: ledger-client-in-process
    return Client(base, {}, 10)


def explicit_http(base: str) -> ResourceClient:
    # ruleid: ledger-client-in-process
    return ResourceClient(base, {}, 10, transport=None)


def explicit_http_positional(base: str) -> ResourceClient:
    # ruleid: ledger-client-in-process
    return api_client.ResourceClient(base, {}, 10, None)


def over_http_variable(base: str) -> ResourceClient:
    made = ResourceClient
    # ruleid: ledger-client-in-process
    return made(base, {}, 10)


def over_http_module_variable(base: str) -> ResourceClient:
    made = api_client.ResourceClient
    # ruleid: ledger-client-in-process
    return made(base, {}, 10)


def in_process(base: str) -> ResourceClient:
    # ok: ledger-client-in-process
    return ResourceClient(base, {}, 10, transport=database_ledger.request)


def in_process_module(base: str) -> ResourceClient:
    # ok: ledger-client-in-process
    return api_client.ResourceClient(base, {}, 10, transport=database_ledger.request)


def in_process_positional(base: str) -> ResourceClient:
    # ok: ledger-client-in-process
    return Client(base, {}, 10, database_ledger.request)


def in_process_variable(base: str) -> ResourceClient:
    made = ResourceClient
    # ok: ledger-client-in-process
    return made(base, {}, 10, transport=database_ledger.request)
