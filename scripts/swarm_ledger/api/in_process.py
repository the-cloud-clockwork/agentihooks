import io
import json
import os
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from email.message import Message
from typing import Any
from urllib.parse import quote

from ..repository.postgres import URL
from .client import ResourceClient, writer_header

__all__ = ["ACTIVE", "InProcessClient", "active", "connect", "scoped"]

ACTIVE: ContextVar[bool] = ContextVar("ledger_in_process", default=False)


def active() -> bool:
    """True inside a tick pass whose ledger requests are served in process."""
    return ACTIVE.get()


@contextmanager
def scoped(environ: dict[str, str] | None = None) -> Iterator[None]:
    """Serve ledger requests in process while the database URL is set."""
    token = ACTIVE.set(bool((os.environ if environ is None else environ).get(URL)))
    try:
        yield
    finally:
        ACTIVE.reset(token)


def connect(base: str, credentials: dict[str, str], timeout: float = 10) -> ResourceClient:
    """The ledger client for these settings."""
    if active():
        return InProcessClient(base, credentials, timeout)
    return ResourceClient(base, credentials, timeout)


class _Request:
    """One ledger API request."""

    def __init__(self, command: str, path: str, headers: Message, raw: bytes, services: Any) -> None:
        self.command = command
        self.path = path
        self.headers = headers
        self.rfile = io.BytesIO(raw)
        self.services = services
        self.reply: tuple[int, bytes] = (500, b'{"error":{"code":"storage_error","message":"no reply"}}')

    def exists(self, slug: str) -> bool:
        return bool(self.services.ledger.core.SLUG_RE.match(slug) and self.services.repository.exists(slug))

    def send(self, status: int, body: str | bytes, kind: str) -> None:
        """Keep the reply."""
        self.reply = (status, body if isinstance(body, bytes) else body.encode())

    def send_export(self, data: bytes) -> None:
        """Keep the export reply."""
        self.reply = (200, data)


class InProcessClient(ResourceClient):
    """Ledger client running the API routes in this process."""

    def request(self, slug: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        from scripts.swarm_ledger import api, ledger_server

        services = ledger_server.api_services()
        raw = b"" if payload is None else json.dumps(payload).encode()
        headers = Message()
        headers["Host"] = next(iter(sorted(services.policy.allowed_hosts)), "")
        headers["X-Ledger-Token"] = services.repository.token(slug) or ""
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(raw))
        for name, value in (writer_header() if path == "operations" else {}).items():
            headers[name] = value
        route = f"/api/v1/ledgers/{quote(slug, safe='')}/{path}"
        handler = _Request("GET" if payload is None else "POST", route, headers, raw, services)
        api.handle(handler, services)
        status, body = handler.reply
        if status >= 400:
            raise urllib.error.HTTPError(f"{self.base}{route}", status, "refused", Message(), io.BytesIO(body))
        reply: dict[str, Any] = json.loads(body)
        return reply
