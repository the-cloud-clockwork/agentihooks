"""A controller write lands within its request budget while another client reads a full export, even when the pass's
short view read timed out and it runs from its last good view; it is queued only when the server stops answering."""

import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import orjson
import pytest

from scripts.swarm import cli, ledger_client
from scripts.swarm.ledger_client import OFFLINE, LedgerClient, LedgerRefused, LedgerUnreachable
from scripts.swarm_ledger.api import client as api
from scripts.tick import view

PAUSE_S = 1.5
SHORT_S = 0.3
REQUEST_S = 1.0
GENERATION = 9
STATE = {"tasks": [{"id": f"t{index}", "state": "open", "text": "x" * 2000} for index in range(200)], "_meta": {}}
STORED = {"tasks": [{"id": "t0", "state": "open"}], "_meta": {"rev": 1, "events": []}, "phases": []}
OP = {"op": "comment", "id": "c1", "by": "swarm", "item": "tasks/t0", "text": "landed"}


class Missing(Exception):
    pass


class Ledger(BaseHTTPRequestHandler):
    """Answer an export read slowly, pausing mid body, and a write at once."""

    exports: list[str] = []
    pause: float = PAUSE_S
    writes: list[Any] = []

    def do_POST(self) -> None:
        """Record the request, then answer an export in two halves around a pause or a write with its state."""
        sent = orjson.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        if self.path.endswith("/write"):
            Ledger.writes.append(sent)
            return self._send(orjson.dumps({"tasks": []}), pause=0)
        Ledger.exports.append("started")
        self._send(orjson.dumps({"generation": GENERATION, "data": STATE}), pause=Ledger.pause)
        Ledger.exports.append("ended")

    def _send(self, body: bytes, pause: float) -> None:
        """Send body with its headers, pausing pause seconds after its first half."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        half = len(body) // 2
        self.wfile.write(body[:half])
        self.wfile.flush()
        time.sleep(pause)
        self.wfile.write(body[half:])

    def log_message(self, format: str, *args: object) -> None:
        """Keep the test output quiet."""


def _post(url: str, payload: object, timeout: float) -> dict[str, Any]:
    """The JSON answer of payload posted to url within timeout seconds."""
    request = urllib.request.Request(url, orjson.dumps(payload), {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return orjson.loads(reply.read())


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A ledger client module over a slow export server for ``sw``, recording what it queues locally."""
    from scripts.scheduler import service

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Ledger)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    queued: list[tuple[str, list[dict[str, Any]], bool]] = []

    def call(slug: str, ops: list[dict[str, Any]] | None, service: bool = False) -> dict[str, Any]:
        """One write to the server under the request budget, exiting as the ledger CLI does when it stops."""
        try:
            return _post(f"{base}/write", ops, REQUEST_S)
        except OSError as exc:
            raise SystemExit(f"ledger server not answering on {base}: {exc}")

    def queue(slug: str, ops: list[dict[str, Any]], service: bool) -> dict[str, Any]:
        """Record ops as queued locally."""
        queued.append((slug, ops, service))
        return {"queued": ops[0]["id"]}

    fake = SimpleNamespace(
        base=lambda slug: base,
        credentials=lambda slug, service: {},
        REQUEST_TIMEOUT=REQUEST_S,
        Missing=Missing,
        call=call,
        queue=queue,
        queued=queued,
        url=base,
    )
    monkeypatch.setattr(ledger_client, "_ledger", lambda: fake)
    monkeypatch.setattr(ledger_client, "SNAPSHOT_TIMEOUT_S", SHORT_S)
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    api.write_export_file(view.view_file(base, "sw", tmp_path), 3, STORED)
    Ledger.exports, Ledger.writes, Ledger.pause = [], [], PAUSE_S
    try:
        yield fake
    finally:
        httpd.shutdown()
        httpd.server_close()


def _reading(base: str) -> threading.Thread:
    """Another client reading a full export, started and seen in flight."""
    reader = threading.Thread(target=_post, args=(f"{base}/export", {}, 30.0), daemon=True)
    reader.start()
    deadline = time.monotonic() + 5
    while not Ledger.exports and time.monotonic() < deadline:
        time.sleep(0.01)
    return reader


@pytest.mark.wall_clock
@pytest.mark.feature("swarm-tick-slow-export-write")
def test_a_write_lands_within_its_budget_while_another_client_reads_a_full_export(ledger: SimpleNamespace) -> None:
    reader = _reading(ledger.url)
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))  # pyright: ignore[reportPrivateUsage]
        assert OFFLINE.get() is True
        assert ledger_client.SLOW.get() is True
        held = view.current()
        assert held is not None and held.generation == 3
        assert Ledger.exports.count("ended") < Ledger.exports.count("started")
        started = time.monotonic()
        LedgerClient(service=True)._call("sw", [OP])  # pyright: ignore[reportPrivateUsage]
        elapsed = time.monotonic() - started
    reader.join(10)
    assert ledger_client.SLOW.get() is False
    assert Ledger.writes == [[OP]]
    assert ledger.queued == []
    assert elapsed < REQUEST_S


@pytest.mark.feature("swarm-tick-slow-export-write")
def test_a_write_the_slow_server_does_not_answer_is_queued_locally(
    ledger: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, list[dict[str, Any]] | None, bool]] = []

    def stopped(slug: str, ops: list[dict[str, Any]] | None, service: bool = False) -> dict[str, Any]:
        """Record the send, then exit as the ledger CLI does when the server stops answering."""
        sent.append((slug, ops, service))
        raise SystemExit(f"ledger server not answering on {ledger.url}: timed out")

    monkeypatch.setattr(ledger, "call", stopped)
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))  # pyright: ignore[reportPrivateUsage]
        answer = LedgerClient(service=True)._call("sw", [OP])  # pyright: ignore[reportPrivateUsage]
    assert ledger_client.SLOW.get() is False
    assert sent == [("sw", [OP], True)]
    assert ledger.queued == [("sw", [OP], True)]
    assert answer["queued"] == "c1"


@pytest.mark.feature("swarm-tick-slow-export-write")
def test_a_write_the_slow_server_refuses_raises_its_refusal(
    ledger: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refusing(slug: str, ops: list[dict[str, Any]] | None, service: bool = False) -> dict[str, Any]:
        """Exit as the ledger CLI does when the server refuses the write."""
        raise SystemExit("server refused: 409 moved")

    monkeypatch.setattr(ledger, "call", refusing)
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(LedgerRefused):
            LedgerClient(service=True)._call("sw", [OP])  # pyright: ignore[reportPrivateUsage]
    assert ledger.queued == []


@pytest.mark.feature("swarm-tick-slow-export-write")
def test_a_pass_whose_server_refused_the_connection_queues_writes_without_sending(
    ledger: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(slug: str, service: bool = False, timeout: float | None = None) -> None:
        """Fail the view read as a server that is down fails it."""
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(cli.ledger_view, "start", refused)
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))  # pyright: ignore[reportPrivateUsage]
        assert OFFLINE.get() is True
        assert ledger_client.SLOW.get() is False
        LedgerClient(service=True)._call("sw", [OP])  # pyright: ignore[reportPrivateUsage]
    assert Ledger.writes == []
    assert ledger.queued == [("sw", [OP], True)]


@pytest.mark.feature("swarm-tick-slow-export-write")
def test_a_mid_pass_read_the_server_refused_queues_writes_without_sending(ledger: SimpleNamespace) -> None:
    stored = view.LedgerView(3, STORED)

    def refused() -> view.LedgerView:
        """Fail the renewed view read as a server that is down fails it."""
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    with view.holding():
        view._hold("sw", stored, view._kept(refused))  # pyright: ignore[reportPrivateUsage]
        view.wrote()
        assert view.current() is stored
        assert view.offline() is True
        assert view.slow() is False
        LedgerClient(service=True)._call("sw", [OP])  # pyright: ignore[reportPrivateUsage]
    assert Ledger.writes == []
    assert ledger.queued == [("sw", [OP], True)]


@pytest.mark.feature("swarm-tick-slow-export-write")
@pytest.mark.parametrize("slow", [True, False], ids=["slow", "refused"])
def test_a_full_outbox_leaves_an_offline_write_unreachable(
    slow: bool, ledger: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def stopped(slug: str, ops: list[dict[str, Any]] | None, service: bool = False) -> dict[str, Any]:
        """Exit as the ledger CLI does when the server stops answering."""
        raise SystemExit(f"ledger server not answering on {ledger.url}: timed out")

    def full(slug: str, ops: list[dict[str, Any]], service: bool) -> dict[str, Any]:
        """Exit as the outbox does when it holds no room for ops."""
        attempts.append(slug)
        raise SystemExit("the local inbox outbox is full")

    def refused(slug: str, service: bool = False, timeout: float | None = None) -> None:
        """Fail the view read as a server that is down fails it."""
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(ledger, "call", stopped)
    monkeypatch.setattr(ledger, "queue", full)
    if not slow:
        monkeypatch.setattr(cli.ledger_view, "start", refused)
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))  # pyright: ignore[reportPrivateUsage]
        assert ledger_client.slow() is slow
        with pytest.raises(LedgerUnreachable, match="outbox is full"):
            LedgerClient(service=True)._call("sw", [OP])  # pyright: ignore[reportPrivateUsage]
    assert attempts == ["sw"]


@pytest.mark.feature("swarm-tick-slow-export-write")
def test_a_write_after_a_mid_pass_view_read_timed_out_is_still_sent(ledger: SimpleNamespace) -> None:
    second = {**OP, "id": "c2"}
    Ledger.pause = 0
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))  # pyright: ignore[reportPrivateUsage]
        assert OFFLINE.get() is False
        Ledger.pause = PAUSE_S
        LedgerClient(service=True)._call("sw", [OP])  # pyright: ignore[reportPrivateUsage]
        held = view.current()
        assert held is not None and held.generation == GENERATION
        assert view.offline() is True
        assert view.slow() is True
        LedgerClient(service=True)._call("sw", [second])  # pyright: ignore[reportPrivateUsage]
    assert Ledger.writes == [[OP], [second]]
    assert ledger.queued == []


@pytest.mark.feature("swarm-tick-slow-export-write")
def test_only_a_timeout_marks_a_failed_read_slow() -> None:
    assert view.timed_out(TimeoutError("timed out")) is True
    assert view.timed_out(urllib.error.URLError(TimeoutError("timed out"))) is True
    assert view.timed_out(urllib.error.URLError(ConnectionRefusedError("refused"))) is False
    assert view.timed_out(ConnectionResetError("reset")) is False
