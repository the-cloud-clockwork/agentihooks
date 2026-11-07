"""Database ledger adapter: the ledger server's own route code run in this process over the shared ledger
repository, so a tick pass with the database URL setting reads and writes its ledger without the ledger server."""

import io
import json
import os
import urllib.error
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from email.message import Message
from typing import Any, cast
from urllib.parse import quote, urlsplit

from scripts.swarm_ledger.api import client as api_client
from scripts.swarm_ledger.api import resources as api_resources
from scripts.swarm_ledger.api import routes as api_routes
from scripts.swarm_ledger.api import task_list
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.api.services import Services
from scripts.swarm_ledger.repository import postgres

__all__ = ["DatabaseResourceClient", "scoped", "serving"]

SERVING: ContextVar[bool] = ContextVar("ledger_in_process", default=False)
SERVICE = ""
routes: Any = api_routes
resources: Any = api_resources
writer_header: Any = cast(Any, api_client).writer_header


def serving() -> bool:
    """Whether ledger calls in this context run in process over the shared database."""
    return SERVING.get()


@contextmanager
def scoped(environ: Mapping[str, str] = os.environ) -> Generator[None]:
    """Serve ledger calls in process while inside, whenever the database URL setting is set."""
    token = SERVING.set(bool(postgres.url(environ)))
    try:
        yield
    finally:
        SERVING.reset(token)


def services() -> Services:
    """The ledger server's API services over its repository, built in this process."""
    from scripts.swarm_ledger import ledger_server

    built: Services = ledger_server.api_services()
    return built


@dataclass
class Exchange:
    """One request handed to the route code, its reply kept in memory."""

    command: str
    path: str
    headers: Message
    rfile: io.BytesIO
    status: int = 200
    reply: bytes = b""

    def send(self, status: int, body: str, kind: str) -> None:
        """Keep a reply the route code sent itself."""
        self.status, self.reply = status, body.encode()

    def send_export(self, data: bytes) -> None:
        """Keep an export the route code sent."""
        self.status, self.reply = 200, data


def answer(found: Services, exchange: Exchange, slug: str, path: str) -> tuple[int, bytes]:
    """The status and body the ledger server sends for this request from the service caller."""
    result: Any
    try:
        if not found.repository.exists(slug):
            raise APIError(404, "ledger_missing", "No such ledger")
        if exchange.command == "GET":
            result = routes.ledger_get(exchange, found, slug, path)
        else:
            result = routes.ledger_operation(exchange, found, slug, path, SERVICE)
        if result is None:
            return exchange.status, exchange.reply
        if isinstance(result, task_list.Page):
            return 200, result.text().encode()
        if not path.endswith("export"):
            resources.bounded(result)
        status = 200
    except APIError as exc:
        refused: Any = exc
        status, result = refused.status, refused.envelope()
    except (OSError, ValueError):
        failed: Any = APIError(500, "storage_error", "Resource could not be read or written")
        status, result = 500, failed.envelope()
    encoded = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return status, encoded.encode()


class DatabaseResourceClient(ResourceClient):
    """A ledger API client whose requests run the ledger server's route code in this process."""

    def request(self, slug: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Answer one ledger API request in process, a refusal raised as the server's HTTP error."""
        encoded = b"" if payload is None else json.dumps(payload).encode()
        headers = Message()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(encoded))
        extra: dict[str, str] = writer_header() if path == "operations" else {}
        for name, value in extra.items():
            headers[name] = value
        url = f"/api/v1/ledgers/{quote(slug, safe='')}/{path}"
        exchange = Exchange("GET" if payload is None else "POST", url, headers, io.BytesIO(encoded))
        status, body = answer(services(), exchange, slug, urlsplit(path).path or "metadata")
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "ledger refused", headers, io.BytesIO(body))
        reply: dict[str, Any] = json.loads(body)
        return reply
