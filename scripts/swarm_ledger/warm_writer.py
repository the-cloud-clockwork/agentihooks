"""The host's warm ledger writer."""

import argparse
import functools
import importlib
import json
import os
import signal
import socket
import socketserver
import sys
import threading
from pathlib import Path
from typing import Any

from scripts.scheduler.service import ROOT, revision
from scripts.swarm_ledger import ledger_server, ports
from scripts.tick.adapters import database_ledger

NAME = "ledger-writer.sock"
RAISED = b"raised"
CHECK_S = 5.0
POLL_S = 0.5
PROBE_S = 1.0
READ_S = 10.0


def socket_path() -> Path:
    """This host's writer socket, in the agentihooks home."""
    config: Any = ports.hooks().config
    return Path(config.AGENTIHOOKS_HOME) / NAME


def connected(timeout: float, where: Path | None = None) -> socket.socket:
    """A connection to the host's ledger writer."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
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
            "principal": database_ledger.principal(),
            "headers": database_ledger.written(path),
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


def raised(exc: Exception) -> bytes:
    """The reply that carries ``exc``, an error the in process call raised, back to the caller."""
    kind = type(exc)
    carried = {"module": kind.__module__, "name": kind.__qualname__, "args": list(exc.args)}
    return RAISED + b"\n" + json.dumps(carried, default=str).encode()


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


class Answering(socketserver.StreamRequestHandler):
    """One ledger API request on the writer socket, answered as the caller it names."""

    timeout = READ_S

    def handle(self) -> None:
        """Reply with the in process answer's status line and body, or with the error the in process call raises."""
        sent = self.rfile.read()
        if not sent:
            return
        asked = json.loads(sent)
        with database_ledger.acting(asked["principal"]):
            try:
                _, status, body = database_ledger.replied(
                    asked["slug"], asked["path"], asked["payload"], asked["headers"]
                )
            except Exception as exc:
                self.wfile.write(raised(exc))
                raise
        self.wfile.write(b"%d\n" % status + body)


def bound(where: Path) -> socketserver.ThreadingUnixStreamServer | None:
    """The owner only writer server at ``where``, None while another writer answers there or is too busy to accept."""
    try:
        connected(PROBE_S, where).close()
        return None
    except TimeoutError:
        return None
    except OSError:
        where.unlink(missing_ok=True)
    previous = os.umask(0o177)
    try:
        return socketserver.ThreadingUnixStreamServer(str(where), Answering)
    finally:
        os.umask(previous)


def moved(loaded: str) -> bool:
    """Whether the checkout this writer runs from moved off the revision it started on."""
    current = revision(ROOT)
    if not current or current == loaded:
        return False
    print(f"the ledger writer {os.getpid()} restarts: the checkout moved from {loaded} to {current}", file=sys.stderr)
    return True


def serve(where: Path, stopped: threading.Event, check_s: float = CHECK_S) -> int:
    """Answer ledger requests on ``where`` until stopped or the checkout moves."""
    server = bound(where)
    if server is None:
        print(f"a ledger writer already answers on {where}", file=sys.stderr)
        return 1
    try:
        loaded = revision(ROOT)
        database_ledger.services()
        listening = threading.Thread(target=server.serve_forever, args=(POLL_S,))
        listening.start()
        try:
            while not stopped.is_set() and not moved(loaded):
                ledger_server.scheduled_wait(check_s, timeout=check_s)
        finally:
            server.shutdown()
            listening.join()
    finally:
        where.unlink(missing_ok=True)
        server.server_close()
    return 0


def main() -> int:
    """``agentihooks ledger writer serve``: run this host's warm ledger writer."""
    parser = argparse.ArgumentParser(prog=sys.argv[0], description="Run this host's warm ledger writer.")
    parser.add_argument("action", choices=["serve"])
    parser.parse_args(sys.argv[1:])
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stopped.set())
    return serve(socket_path(), stopped)
