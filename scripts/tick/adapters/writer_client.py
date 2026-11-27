"""Writer client: the ledger API transport a command line ledger client sends each request through, to this host's
warm writer socket first; it loads the database ledger adapter only to answer a request in process."""

import functools
import importlib
import io
import json
import socket
import sys
import urllib.error
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from email.message import Message
from functools import partial
from pathlib import Path
from typing import Any, cast

from scripts.swarm import timing
from scripts.swarm_ledger import ports
from scripts.swarm_ledger.api import client as api_client
from scripts.swarm_ledger.api.client import ResourceClient

NAME = "ledger-writer.sock"
RAISED = b"raised"
ACTING: ContextVar[str | None] = ContextVar("ledger_acting", default=None)
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


def written(path: str) -> dict[str, str]:
    """The headers a ledger API request from this process carries beside its body: the writer's worktree on
    operations."""
    extra: dict[str, str] = writer_header() if path == "operations" else {}
    return extra


def socket_path() -> Path:
    """This host's writer socket, in the agentihooks home."""
    config: Any = ports.hooks().config
    return Path(config.AGENTIHOOKS_HOME) / NAME


def connected(timeout: float, where: Path | None = None) -> socket.socket:
    """A connection to the host's ledger writer."""
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(timeout)
    try:
        sock.connect(str(where or socket_path()))
    except OSError:
        sock.close()
        raise
    return sock


def ask(sock: socket.socket, slug: str, path: str, payload: dict[str, Any] | None) -> tuple[int, bytes]:
    """The writer's status and body for one ledger API request sent on ``sock`` as this process's caller; an error
    the writer's in process call raised is raised again here."""
    with sock:
        asked = {
            "slug": slug,
            "path": path,
            "payload": payload,
            "principal": principal(),
            "headers": written(path),
        }
        sock.sendall(json.dumps(asked).encode())
        sock.shutdown(socket.SHUT_WR)
        with sock.makefile("rb") as reply:
            head, body = reply.readline(), reply.read()
    if not head.endswith(b"\n"):
        raise EOFError("the ledger writer closed with no answer")
    if head == RAISED + b"\n":
        raise reraised(body)
    return int(head), body


def reraised(body: bytes) -> Exception:
    """The error a ``raised`` reply carries, rebuilt as the error class the writer raised, else a runtime error naming
    it; only a loaded module or one of agentihooks' own is imported, and only an error class is called."""
    carried = json.loads(body)
    module, name = carried["module"], carried["name"]
    fallback = RuntimeError(f"the ledger writer raised {module}.{name}: {carried['args']}")
    if module not in sys.modules and not module.startswith("scripts."):
        return fallback
    try:
        kind: Any = functools.reduce(getattr, name.split("."), importlib.import_module(module))
        if not (isinstance(kind, type) and issubclass(kind, Exception)):
            return fallback
        return kind(*carried["args"])
    except Exception as exc:
        fallback.__cause__ = exc
        return fallback


def decoded(slug: str, path: str, headers: Message, status: int, body: bytes) -> dict[str, Any]:
    """The reply one ledger API answer carries, a refusal raised as the server's HTTP error."""
    if status >= 400:
        url = f"/api/v1/ledgers/{slug}/{path}"
        raise urllib.error.HTTPError(url, status, "ledger refused", headers, io.BytesIO(body))
    reply: dict[str, Any] = json.loads(body)
    return reply


def in_process(slug: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Answer one ledger API request in process through the database ledger adapter, imported only here."""
    from scripts.tick.adapters import database_ledger

    return database_ledger.request(slug, path, payload)


def routed(timeout: float, slug: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Answer one ledger API request through this host's warm writer, or in process inside a tick pass or when no
    writer takes the connection; a request the writer took is never answered again here, its lost reply raised as a
    timeout."""
    if timing.PASS.get() is not None:
        return in_process(slug, path, payload)
    try:
        sock = connected(timeout)
    except OSError:
        return in_process(slug, path, payload)
    with sock:
        try:
            status, body = ask(sock, slug, path, payload)
        except (OSError, EOFError, ValueError) as exc:
            raise TimeoutError(f"the ledger writer took {path} on {slug} and sent no answer") from exc
    return decoded(slug, path, Message(), status, body)


def client(base: str, timeout: float) -> ResourceClient:
    """A ledger API client whose requests go to this host's warm writer first and otherwise run the ledger server's
    route code in this process."""
    return ResourceClient(base, {}, timeout, transport=partial(routed, timeout))
