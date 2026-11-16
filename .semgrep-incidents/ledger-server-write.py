from http.server import BaseHTTPRequestHandler

from scripts.swarm_ledger.api import client as hub_client
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.tick.adapters import database_ledger


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)

    # ruleid: ledger-server-write-route
    def do_POST(self) -> None:
        self.send_response(200)

    # ruleid: ledger-server-write-route
    def do_PUT(self) -> None:
        self.send_response(200)

    # ruleid: ledger-server-write-route
    def do_PATCH(self) -> None:
        self.send_response(200)

    # ruleid: ledger-server-write-route
    def do_DELETE(self) -> None:
        self.send_response(200)

    # ok: ledger-server-write-route
    def do_HEAD(self) -> None:
        self.send_response(200)


class WriteHandler(BaseHTTPRequestHandler):
    # ok: ledger-server-write-route
    def do_POST(self) -> None:
        self.send_response(200)


def over_http(base: str, headers: dict[str, str]) -> ResourceClient:
    # ruleid: ledger-http-client
    return ResourceClient(base, headers, 10)


def hosted(base: str, credential: str) -> object:
    # ruleid: ledger-http-client
    return hub_client.ResourceClient(base, {"X-Controller-Credential": credential}, 10)


def in_process(base: str) -> ResourceClient:
    # ok: ledger-http-client
    return ResourceClient(base, {}, 10, transport=database_ledger.request)


def in_process_module(base: str) -> object:
    # ok: ledger-http-client
    return hub_client.ResourceClient(base, {}, 10, transport=database_ledger.request)


def switched(slug: str) -> object:
    # ruleid: ledger-write-switch
    if database_ledger.serving():
        return database_ledger.client(slug, 10)
    return None


def scoped_pass(slug: str) -> None:
    # ruleid: ledger-write-switch
    with database_ledger.scoped():
        return None


def acted(slug: str) -> None:
    # ok: ledger-write-switch
    with database_ledger.acting(""):
        return None
