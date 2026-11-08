"""A pass whose short ledger read times out on a large export reads it again under the export budget unless this host
stores a view fresh enough to run from."""

import io
import os
import threading
import time
import urllib.error
from collections.abc import Iterator
from contextlib import ExitStack
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import orjson
import pytest

from scripts.swarm import cli, ledger_client
from scripts.swarm.ledger_client import OFFLINE, LedgerClient
from scripts.swarm_ledger.api import client as api
from scripts.tick import view

PAUSE_S = 1.0
SHORT_S = 0.3
LONG_S = 10.0
GENERATION = 9
TASKS = [{"id": f"t{index}", "state": "open", "text": "x" * 2000} for index in range(400)]
STATE = {"tasks": TASKS, "_meta": {"rev": 4, "events": []}, "phases": [{"id": "p1", "done": False}]}
STORED = {"tasks": TASKS[:1], "_meta": {"rev": 1, "events": []}, "phases": []}


class SlowExport(BaseHTTPRequestHandler):
    """Answer every export read in full, pausing mid body longer than the short read budget."""

    reads: list[float] = []

    def do_POST(self) -> None:
        """Send the export headers and half the body, pause, then send the rest."""
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        SlowExport.reads.append(time.monotonic())
        body = orjson.dumps({"generation": GENERATION, "data": STATE})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        half = len(body) // 2
        self.wfile.write(body[:half])
        self.wfile.flush()
        time.sleep(PAUSE_S)
        self.wfile.write(body[half:])

    def log_message(self, format: str, *args: object) -> None:
        """Keep the test output quiet."""


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A slow ledger server for ``sw`` and the path this host stores its view of ``sw`` at."""
    from scripts.scheduler import service

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), SlowExport)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    ledger = SimpleNamespace(
        base=lambda slug: base if slug == "sw" else "http://127.0.0.1:9",
        credentials=lambda slug, service: {},
        REQUEST_TIMEOUT=SHORT_S,
    )
    monkeypatch.setattr(ledger_client, "_ledger", lambda: ledger)
    monkeypatch.setattr(ledger_client, "SNAPSHOT_TIMEOUT_S", SHORT_S)
    monkeypatch.setattr(ledger_client, "EXPORT_TIMEOUT_S", LONG_S, raising=False)
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    SlowExport.reads = []
    try:
        yield view.view_file(base, "sw", tmp_path)
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.feature("swarm-tick-slow-export")
def test_a_first_full_read_slower_than_the_short_budget_is_read_again_and_stored(server: Path) -> None:
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))  # pyright: ignore[reportPrivateUsage]
        assert OFFLINE.get() is False
        held = view.current()
        assert held is not None and held.generation == GENERATION
    assert len(SlowExport.reads) == 2
    assert api.read_export_file(server) == (GENERATION, STATE)


@pytest.mark.feature("swarm-tick-slow-export")
def test_a_stale_stored_view_is_read_again_under_the_export_budget(server: Path) -> None:
    api.write_export_file(server, 3, STORED)
    past = time.time() - view.FRESH_VIEW_S - 60
    os.utime(server, (past, past))
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))  # pyright: ignore[reportPrivateUsage]
        assert OFFLINE.get() is False
        held = view.current()
        assert held is not None and held.generation == GENERATION
    assert api.read_export_file(server) == (GENERATION, STATE)


@pytest.mark.feature("swarm-tick-slow-export")
def test_a_fresh_stored_view_runs_the_pass_offline_without_a_long_read(server: Path) -> None:
    api.write_export_file(server, 3, STORED)
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))  # pyright: ignore[reportPrivateUsage]
        assert OFFLINE.get() is True
        held = view.current()
        assert held is not None and held.generation == 3
    assert len(SlowExport.reads) == 1


@pytest.mark.feature("swarm-tick-slow-export")
def test_a_stored_view_is_fresh_only_until_its_age_reaches_the_limit(
    server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = 1_000_000.0
    api.write_export_file(server, 3, STORED)
    os.utime(server, (written, written))
    now = [written + view.FRESH_VIEW_S - 1]
    monkeypatch.setattr(view.time, "time", lambda: now[0])
    assert view.fresh("sw") is True
    now[0] = written + view.FRESH_VIEW_S
    assert view.fresh("sw") is False
    assert view.fresh("other") is False


@pytest.mark.feature("swarm-tick-slow-export")
def test_a_long_read_that_also_times_out_runs_the_pass_offline_on_the_stale_view(
    server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger_client, "EXPORT_TIMEOUT_S", SHORT_S)
    api.write_export_file(server, 3, STORED)
    past = time.time() - view.FRESH_VIEW_S - 60
    os.utime(server, (past, past))
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))  # pyright: ignore[reportPrivateUsage]
        assert OFFLINE.get() is True
        held = view.current()
        assert held is not None and held.generation == 3
    assert len(SlowExport.reads) == 2


def _refused(code: int) -> urllib.error.HTTPError:
    """An HTTP refusal of the export read with status ``code``."""
    return urllib.error.HTTPError("http://ledger/export", code, "refused", Message(), io.BytesIO(b"{}"))


@pytest.mark.feature("swarm-tick-slow-export")
@pytest.mark.parametrize(("code", "offline"), [(500, True), (499, None)], ids=["failing", "refusing"])
def test_the_long_read_runs_as_the_pass_service_and_a_refusal_ends_the_pass(
    code: int, offline: bool | None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, bool, float | None]] = []
    replies: list[OSError] = [TimeoutError("timed out"), _refused(code)]

    def start(slug: str, service: bool = False, timeout: float | None = None) -> None:
        calls.append((slug, service, timeout))
        raise replies.pop(0)

    monkeypatch.setattr(cli.ledger_view, "start", start)
    monkeypatch.setattr(cli.ledger_view, "fresh", lambda slug: False)
    monkeypatch.setattr(cli.ledger_view, "hold_stored", lambda slug: view.LedgerView(3, STORED))
    with ExitStack() as beating:
        if offline is None:
            with pytest.raises(urllib.error.HTTPError):
                cli._hold_view(beating, "sw", LedgerClient(service=True))  # pyright: ignore[reportPrivateUsage]
        else:
            cli._hold_view(beating, "sw", LedgerClient(service=True))  # pyright: ignore[reportPrivateUsage]
            assert OFFLINE.get() is offline
            assert f"ledger sw not answering (HTTP Error {code}: refused)" in capsys.readouterr().err
    assert calls == [
        ("sw", True, ledger_client.SNAPSHOT_TIMEOUT_S),
        ("sw", True, ledger_client.EXPORT_TIMEOUT_S),
    ]
