"""Database ledger adapter: the ledger API route code run in this process over the shared ledger repository, the one
path every swarm, agent and service client reads and writes a ledger through; the ledger server only reads."""

import io
import json
import logging
import sys
import time
import urllib.error
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from email.message import Message
from typing import Any, cast
from urllib.parse import urlsplit

from scripts.swarm import timing
from scripts.swarm_ledger.api import client as api_client
from scripts.swarm_ledger.api import resources as api_resources
from scripts.swarm_ledger.api import routes as api_routes
from scripts.swarm_ledger.api import task_list
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.api.services import Services
from scripts.swarm_ledger.api_service import Writes

__all__ = ["acting", "client", "exchanged", "principal", "request", "upload"]

ACTING: ContextVar[str | None] = ContextVar("ledger_acting", default=None)
routes: Any = api_routes
resources: Any = api_resources
writer_header: Any = cast(Any, api_client).writer_header


def principal() -> str:
    """The caller the route code judges a call by: the name set by ``acting``, else a pinned agent by its name, else
    the swarm's own service, the empty name."""
    from scripts.gates import Who

    named = ACTING.get()
    if named is not None:
        return named
    who: Any = cast(Any, Who).from_env()
    return str(who.name) if who.pinned else ""


@contextmanager
def acting(name: str) -> Generator[None]:
    """Judge every ledger call inside as ``name``; the empty name is the swarm's own service, as a tick pass."""
    token = ACTING.set(name)
    try:
        yield
    finally:
        ACTING.reset(token)


def services() -> Services:
    """The ledger API services with every write hook, built in this process over the shared repository."""
    from scripts.swarm_ledger import api_service

    return api_service.services()


@dataclass
class Exchange(Writes):
    """One request handed to the route code, its reply kept in memory, judged as ``principal`` names."""

    command: str
    path: str
    headers: Message
    rfile: io.BytesIO
    status: int = 200
    reply: bytes = b""

    def send(self, code: int, body: str, ctype: str) -> None:
        """Keep a reply the route code sent itself."""
        self.status, self.reply = code, body.encode()

    def send_export(self, data: bytes) -> None:
        """Keep an export the route code sent."""
        self.status, self.reply = 200, data

    def exists(self, slug: str) -> bool:
        """Whether the shared repository holds the ledger."""
        return bool(services().repository.exists(slug))

    def refused(self, slug: str | None = None) -> bool:
        """Admit the caller as the principal this process acts as, refusing a request that names another agent than
        the one it acts as, as the credential check refuses it over HTTP."""
        self.principal = principal()
        claimed = self.headers.get("X-Ledger-Agent")
        if not self.principal or claimed in (None, self.principal):
            return False
        forbidden: Any = APIError(403, "forbidden", f"this process acts as {self.principal}, not {claimed}")
        self.status, self.reply = forbidden.status, json.dumps(forbidden.envelope()).encode()
        return True


def settled(exchange: Exchange, path: str, route: Callable[[], Any]) -> tuple[int, bytes]:
    """The status and body the ledger API service sends for one request ``route`` answers."""
    result: Any
    try:
        result = route()
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
        logging.getLogger(__name__).exception("Ledger API storage failure")
        failed: Any = APIError(500, "storage_error", "Resource could not be read or written")
        status, result = failed.status, failed.envelope()
    encoded = result if isinstance(result, str) else json.dumps(result)
    return status, encoded.encode()


def answer(found: Services, exchange: Exchange, slug: str, path: str) -> tuple[int, bytes]:
    """The status and body the ledger API service sends for this request from the caller ``principal`` names."""

    def route() -> Any:
        if not found.repository.exists(slug):
            raise APIError(404, "ledger_missing", "No such ledger")
        if exchange.command == "GET":
            return routes.ledger_get(exchange, found, slug, path)
        return routes.ledger_operation(exchange, found, slug, path, principal())

    return settled(exchange, path, route)


def answered(slug: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Answer one ledger API request in process, a refusal raised as the server's HTTP error."""
    encoded = b"" if payload is None else json.dumps(payload).encode()
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(len(encoded))
    extra: dict[str, str] = writer_header() if path == "operations" else {}
    for name, value in extra.items():
        headers[name] = value
    url = f"/api/v1/ledgers/{slug}/{path}"
    exchange = Exchange("GET" if payload is None else "POST", url, headers, io.BytesIO(encoded))
    status, body = answer(services(), exchange, slug, urlsplit(path).path or "metadata")
    if status >= 400:
        raise urllib.error.HTTPError(url, status, "ledger refused", headers, io.BytesIO(body))
    reply: dict[str, Any] = json.loads(body)
    return reply


def request(slug: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Answer one ledger API request in process as ``answered`` does; inside a tick pass, also journal one
    ``swarm_tick_ledger_call`` line naming its ledger, method, path and outcome with its wall seconds."""
    current = timing.PASS.get()
    if current is None:
        return answered(slug, path, payload)
    started = time.monotonic()
    outcome = "error"
    try:
        reply = answered(slug, path, payload)
        outcome = "ok"
        return reply
    except urllib.error.HTTPError as exc:
        outcome = str(exc.code)
        raise
    finally:
        line = {
            "event": "swarm_tick_ledger_call",
            "slug": timing.SWARM.get(),
            "pass_id": current["pass_id"],
            "ledger": slug,
            "method": "GET" if payload is None else "POST",
            "path": urlsplit(path).path,
            "outcome": outcome,
            "wall_s": time.monotonic() - started,
        }
        print(json.dumps(line), file=sys.stderr)


def client(base: str, timeout: float) -> ResourceClient:
    """A ledger API client whose requests run the ledger server's route code in this process."""
    return ResourceClient(base, {}, timeout, transport=request)


def upload(slug: str, route: str, data: bytes, headers: Mapping[str, str]) -> dict[str, Any]:
    """Store one upload on the ledger in process through its upload route, a refusal raised as the service's HTTP
    error."""
    sent = Message()
    for name, value in headers.items():
        sent[name] = value
    sent["Content-Length"] = str(len(data))
    url = f"/api/v1/ledgers/{slug}/uploads/{route}"
    exchange = Exchange("POST", url, sent, io.BytesIO(data))
    status, body = answer(services(), exchange, slug, f"uploads/{route}")
    if status >= 400:
        raise urllib.error.HTTPError(url, status, "ledger refused", sent, io.BytesIO(body))
    reply: dict[str, Any] = json.loads(body)
    return reply


def exchanged(command: str, path: str, payload: dict[str, Any] | None, headers: Mapping[str, str]) -> dict[str, Any]:
    """Answer one ledger API request in process as the ledger API service does, judged by the credentials in
    ``headers``; a refusal raised as the service's HTTP error."""
    found = services()
    encoded = b"" if payload is None else json.dumps(payload).encode()
    sent = Message()
    sent["Host"] = next(iter(found.policy.allowed_hosts))
    sent["Content-Type"] = "application/json"
    sent["Content-Length"] = str(len(encoded))
    for name, value in headers.items():
        sent[name] = value
    exchange = Exchange(command, path, sent, io.BytesIO(encoded))
    status, body = settled(exchange, urlsplit(path).path, lambda: routes.dispatch(exchange, found))
    if status >= 400:
        raise urllib.error.HTTPError(path, status, "ledger refused", sent, io.BytesIO(body))
    reply: dict[str, Any] = json.loads(body)
    return reply
