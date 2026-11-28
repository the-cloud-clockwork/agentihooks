"""The host's warm ledger writer."""

import argparse
import contextlib
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
from scripts.swarm_ledger import ledger_server, request_metrics
from scripts.tick.adapters import database_ledger
from scripts.tick.adapters.writer_client import RAISED, connected, socket_path

CHECK_S = 5.0
PROBE_S = 1.0
READ_S = 10.0


def raised(exc: Exception) -> bytes:
    """The reply that carries ``exc``, an error the in process call raised, back to the caller."""
    kind = type(exc)
    carried = {"module": kind.__module__, "name": kind.__qualname__, "args": list(exc.args)}
    return RAISED + b"\n" + json.dumps(carried, default=str).encode()


class Answering(socketserver.StreamRequestHandler):
    """One ledger API request on the writer socket, answered as the caller it names."""

    timeout = READ_S

    def handle(self) -> None:
        """Reply with the in process answer's status line and body, or with the error the in process call raises; a
        write keeps one request metric sample once its reply is sent and the connection closed for writing."""
        sent = self.rfile.read()
        if not sent:
            return
        asked = json.loads(sent)
        method = "GET" if asked["payload"] is None else "POST"
        with request_metrics.answering(method, f"/api/v1/ledgers/{asked['slug']}/{asked['path']}"):
            try:
                self.answer(asked)
            finally:
                with contextlib.suppress(OSError):
                    self.connection.shutdown(socket.SHUT_WR)

    def answer(self, asked: dict[str, Any]) -> None:
        """Write the in process answer to ``asked``, or the error the in process call raises, as this request's reply."""
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
        listening = threading.Thread(target=server.serve_forever)
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
    parser = argparse.ArgumentParser(description="Run this host's warm ledger writer.")
    parser.add_argument("action", choices=["serve"])
    parser.parse_args()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stopped.set())
    return serve(socket_path(), stopped)
