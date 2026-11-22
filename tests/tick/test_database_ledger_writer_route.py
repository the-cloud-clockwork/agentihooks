"""The database ledger client sends each request to this host's warm writer first, runs it in process when no writer
takes the connection or inside a tick pass, and never runs a request the writer took a second time."""

import json
import os
import socket
import threading
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from hooks import config
from scripts.swarm import timing
from scripts.swarm_ledger import warm_writer
from scripts.tick.adapters import database_ledger

pytestmark = [pytest.mark.unit]

IN_PROCESS = {"data": {"answered": "in process"}}


class Answers:
    """The in process route code, recording every request it answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, slug: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((slug, path, payload))
        return IN_PROCESS


@pytest.fixture
def home(memory_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A short agentihooks home on tmpfs, so the writer socket path fits a unix socket address."""
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", memory_path)
    monkeypatch.chdir(memory_path)
    return memory_path


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Answers:
    """The in process route code replaced by a recorder."""
    recorder = Answers()
    monkeypatch.setattr(database_ledger, "answered", recorder)
    return recorder


@pytest.fixture
def released() -> Iterator[threading.Event]:
    """An event a held writer waits on before it closes, set when the test ends."""
    event = threading.Event()
    yield event
    event.set()


def writer(where: Path, reply: bytes, released: threading.Event | None = None) -> tuple[threading.Thread, list[bytes]]:
    """A one request writer on ``where`` that records what it is sent, then answers ``reply``, or holds the
    connection until ``released`` and closes it unanswered."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(where))
    listener.listen(1)
    heard: list[bytes] = []

    def answer() -> None:
        with listener, listener.accept()[0] as conn, conn.makefile("rb") as sent:
            heard.append(sent.read())
            if released is not None:
                released.wait(10.0)
                return
            conn.sendall(reply)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread, heard


def asked(slug: str, path: str, payload: dict[str, Any] | None, principal: str, headers: dict[str, str]) -> bytes:
    """The bytes the client sends the writer for one request."""
    sent = {"slug": slug, "path": path, "payload": payload, "principal": principal, "headers": headers}
    return json.dumps(sent).encode()


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_request_goes_to_the_host_writer_when_one_listens(home: Path, answers: Answers) -> None:
    thread, heard = writer(warm_writer.socket_path(), b'201\n{"data": {"answered": "writer"}}')
    with database_ledger.acting("eng@ab-1"):
        reply = database_ledger.client("in-process", 5.0).request("sw", "operations", {"ops": []})
    thread.join(10.0)
    assert reply == {"data": {"answered": "writer"}}
    assert answers.calls == []
    worktree = {"X-Ledger-Worktree": quote(os.getcwd())}
    assert heard == [asked("sw", "operations", {"ops": []}, "eng@ab-1", worktree)]


@pytest.mark.feature("swarm-tick-database-ledger")
@pytest.mark.parametrize("left", ["absent", "stale"])
def test_a_request_runs_in_process_when_no_writer_takes_the_connection(home: Path, answers: Answers, left: str) -> None:
    if left == "stale":
        gone = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        gone.bind(str(warm_writer.socket_path()))
        gone.close()
    reply = database_ledger.client("in-process", 5.0).request("sw", "tasks")
    assert reply == IN_PROCESS
    assert answers.calls == [("sw", "tasks", None)]
    assert warm_writer.socket_path().exists() is (left == "stale")


@pytest.mark.feature("swarm-tick-database-ledger")
@pytest.mark.parametrize(
    ("reply", "cause"),
    [(b"", EOFError), (b"201", EOFError), (b'two\n{"data": {}}', ValueError)],
    ids=["closed", "no-status-line", "unreadable-status"],
)
def test_a_lost_reply_after_send_raises_a_timeout_and_never_runs_in_process(
    home: Path, answers: Answers, reply: bytes, cause: type[BaseException]
) -> None:
    thread, heard = writer(warm_writer.socket_path(), reply)
    with database_ledger.acting(""), pytest.raises(TimeoutError) as lost:
        database_ledger.client("in-process", 5.0).request("sw", "operations", {"ops": []})
    thread.join(10.0)
    assert str(lost.value) == "the ledger writer took operations on sw and sent no answer"
    assert type(lost.value.__cause__) is cause
    assert answers.calls == []
    assert heard == [asked("sw", "operations", {"ops": []}, "", {"X-Ledger-Worktree": quote(os.getcwd())})]


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_writer_that_holds_the_reply_past_the_client_timeout_is_never_run_in_process(
    home: Path, answers: Answers, released: threading.Event
) -> None:
    thread, heard = writer(warm_writer.socket_path(), b"", released)
    with database_ledger.acting(""), pytest.raises(TimeoutError) as lost:
        database_ledger.client("in-process", 0.2).request("sw", "tasks")
    released.set()
    thread.join(10.0)
    assert str(lost.value) == "the ledger writer took tasks on sw and sent no answer"
    assert type(lost.value.__cause__) is TimeoutError
    assert answers.calls == []
    assert heard == [asked("sw", "tasks", None, "", {})]


@pytest.mark.feature("swarm-tick-database-ledger")
@pytest.mark.parametrize("status", [400, 409])
def test_a_writer_refusal_raises_the_in_process_http_error(home: Path, answers: Answers, status: int) -> None:
    body = b'{"error": {"code": "revision_conflict"}}'
    thread, _ = writer(warm_writer.socket_path(), b"%d\n" % status + body)
    with pytest.raises(urllib.error.HTTPError) as refused:
        database_ledger.client("in-process", 5.0).request("sw", "operations", {"ops": []})
    thread.join(10.0)
    assert (refused.value.code, refused.value.url, refused.value.msg) == (
        status,
        "/api/v1/ledgers/sw/operations",
        "ledger refused",
    )
    assert refused.value.read() == body
    assert answers.calls == []


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_reply_just_below_400_from_the_writer_is_returned(home: Path, answers: Answers) -> None:
    thread, _ = writer(warm_writer.socket_path(), b'399\n{"data": {"answered": "writer"}}')
    reply = database_ledger.client("in-process", 5.0).request("sw", "tasks")
    thread.join(10.0)
    assert reply == {"data": {"answered": "writer"}}
    assert answers.calls == []


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_tick_pass_stays_in_process_and_journals_while_a_writer_listens(
    home: Path, answers: Answers, capsys: pytest.CaptureFixture[str]
) -> None:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(warm_writer.socket_path()))
    listener.listen(1)
    token = timing.PASS.set({"pass_id": "p1", "failed_steps": []})
    try:
        with timing.tick("sw"):
            reply = database_ledger.client("in-process", 5.0).request("sw", "tasks")
    finally:
        timing.PASS.reset(token)
    assert reply == IN_PROCESS
    assert answers.calls == [("sw", "tasks", None)]
    journaled = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert [(line["event"], line["path"], line["outcome"]) for line in journaled] == [
        ("swarm_tick_ledger_call", "tasks", "ok")
    ]
    listener.setblocking(False)
    with listener, pytest.raises(BlockingIOError):
        listener.accept()
