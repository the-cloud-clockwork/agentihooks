"""The warm ledger writer answers a ledger API request on its unix socket with the in process answer, as the caller
the request names, and leaves the same rows the in process call leaves."""

import json
import os
import signal
import socket
import stat
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import quote

import pytest

from hooks import config
from scripts.swarm_ledger import api_service, ledger_server, run, storage, warm_writer
from scripts.tick.adapters import database_ledger

CONTENT = {"title": "Warm", "overview": "o", "phases": [{"title": "One", "description": "d"}]}
TASK = {
    "op": "task_add",
    "id": "warm-task",
    "by": "swarm",
    "task": "t1",
    "title": "Warm task",
    "lane": "eng",
    "phase": "p1",
}
COMMENT = {"op": "add", "id": "warm-comment", "by": "swarm", "thread": "phases/p1/comments", "text": "Landed"}
CLOCKS = frozenset({"at", "created_at", "updated_at"})
STALE = "0" * 64


@dataclass
class Twins:
    """Two ledgers made from the same content, and the write hooks each one fired."""

    here: str
    there: str
    fired: list[tuple[str, str]] = field(default_factory=list)

    def hooks(self, slug: str) -> list[str]:
        """The write hooks fired for ``slug``, in order."""
        return [hook for hook, fired in self.fired if fired == slug]


@pytest.fixture
def twins(monkeypatch: pytest.MonkeyPatch) -> Iterator[Twins]:
    """Twin ledgers in the shared test store, their inbox relay, doctor phrase and alert hooks recorded."""
    stem = f"warm-{uuid.uuid4().hex[:8]}"
    found = Twins(f"{stem}-here", f"{stem}-there")
    for slug in (found.here, found.there):
        storage.default().create(slug, CONTENT)
    for hook in ("relay_to_inbox", "doctor_phrase", "deliver_alerts"):
        monkeypatch.setattr(api_service, hook, lambda slug, state, hook=hook: found.fired.append((hook, slug)))
    monkeypatch.setattr(ledger_server.authority, "fence", lambda slug, epoch: None)
    with (
        patch("hooks._redis.get_redis", return_value=None),
        patch("scripts.gates.talk.Budget._marks", return_value=None),
    ):
        yield found


def same_revision(root: Path | str) -> str:
    """The revision of a checkout that never moves."""
    assert root == warm_writer.ROOT
    return "same"


def paced(interval: float, timeout: float | None = None) -> None:
    """A scheduler wait bounded by its own interval that sleeps it, as the scheduler does when unavailable."""
    assert timeout == interval == 0.05
    time.sleep(interval)


def still(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve from a checkout that never moves, paced by a sleeping scheduler wait."""
    still(monkeypatch)
    monkeypatch.setattr(ledger_server, "scheduled_wait", paced)


def serving(where: Path, stopped: threading.Event) -> threading.Thread:
    """A writer serving on ``where`` in a thread until ``stopped``, its socket bound."""
    served = threading.Thread(target=warm_writer.serve, args=(where, stopped, 0.05), daemon=True)
    served.start()
    deadline = time.monotonic() + 10.0
    while not where.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return served


@pytest.fixture
def writer(memory_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A warm writer answering on a socket in a short tmpfs folder."""
    still(monkeypatch)
    where = memory_path / "w.sock"
    stopped = threading.Event()
    served = serving(where, stopped)
    yield where
    stopped.set()
    served.join(10.0)


def over_socket(where: Path, slug: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, bytes]:
    """The status and body the writer on ``where`` answers one request with."""
    return warm_writer.ask(warm_writer.connected(10.0, where), slug, path, payload)


def in_process(slug: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, bytes]:
    """The status and body the in process call answers the same request with."""
    _, status, body = database_ledger.replied(slug, path, payload, database_ledger.written(path))
    return status, body


def unclocked(value: Any) -> Any:
    """``value`` with every clock reading blanked, since twin ledgers are written moments apart."""
    if isinstance(value, dict):
        return {key: None if key in CLOCKS else unclocked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unclocked(item) for item in value]
    return value


def state(slug: str) -> Any:
    """The ledger state the shared store holds, its clock readings blanked."""
    held, _ = storage.default().apply_ops(slug)
    return unclocked(held)


def events(slug: str) -> list[tuple[Any, ...]]:
    """The event rows the shared store holds for ``slug``, their clock readings blanked."""
    repository: Any = storage.default()
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT key, revision, position, value FROM events WHERE slug=? ORDER BY position", (slug,)
        ).fetchall()
    return [(key, revision, position, unclocked(json.loads(value))) for key, revision, position, value in rows]


def raw(where: Path, sent: bytes) -> bytes:
    """Everything the writer on ``where`` replies to ``sent``."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10.0)
        sock.connect(str(where))
        sock.sendall(sent)
        sock.shutdown(socket.SHUT_WR)
        with sock.makefile("rb") as reply:
            return reply.read()


def recording(where: Path, reply: bytes) -> tuple[threading.Thread, list[bytes]]:
    """A one request server on ``where`` that records what it is sent and answers ``reply``."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(where))
    listener.listen(1)
    listener.settimeout(10.0)
    heard: list[bytes] = []

    def answer() -> None:
        with listener, listener.accept()[0] as conn, conn.makefile("rb") as sent:
            heard.append(sent.read())
            conn.sendall(reply)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread, heard


@pytest.mark.feature("ledger-warm-writer")
def test_a_socket_write_returns_the_in_process_reply_and_leaves_the_same_rows(twins: Twins, writer: Path) -> None:
    payload = {"operation_id": "warm-op", "ops": [TASK, COMMENT], "guards": {}}
    with database_ledger.acting(""):
        expected = in_process(twins.here, "operations", payload)
        answered = over_socket(writer, twins.there, "operations", payload)
        replayed = in_process(twins.here, "operations", payload)
        answered_again = over_socket(writer, twins.there, "operations", payload)
    assert expected[0] == 200
    assert answered == expected
    assert answered_again == replayed == expected
    assert state(twins.there) == state(twins.here)
    assert events(twins.there) == events(twins.here)
    assert len(events(twins.here)) == 2
    assert twins.hooks(twins.there) == twins.hooks(twins.here) != []


@pytest.mark.feature("ledger-warm-writer")
def test_a_socket_read_returns_the_in_process_body(twins: Twins, writer: Path) -> None:
    with database_ledger.acting(""):
        in_process(twins.here, "operations", {"operation_id": "seed", "ops": [TASK], "guards": {}})
        expected = in_process(twins.here, "tasks")
        answered = over_socket(writer, twins.here, "tasks")
    assert answered == expected
    assert json.loads(expected[1])["data"][0]["id"] == "t1"


@pytest.mark.feature("ledger-warm-writer")
@pytest.mark.parametrize(
    ("caller", "payload", "status"),
    [
        ("", {"ops": [COMMENT]}, 400),
        ("eng@ab-1", {"operation_id": "warm-op", "ops": [COMMENT], "guards": {}}, 403),
        ("", {"operation_id": "warm-op", "ops": [COMMENT], "guards": {"phases/p1/comments": STALE}}, 409),
    ],
    ids=["schema", "authority", "revision"],
)
def test_a_refused_socket_write_returns_the_in_process_refusal_and_changes_no_row(
    twins: Twins, writer: Path, caller: str, payload: dict[str, Any], status: int
) -> None:
    before = state(twins.there)
    with database_ledger.acting(caller):
        expected = in_process(twins.here, "operations", payload)
        answered = over_socket(writer, twins.there, "operations", payload)
    assert expected[0] == status
    assert answered == expected
    assert state(twins.there) == before
    assert events(twins.there) == events(twins.here) == []


@pytest.mark.feature("ledger-warm-writer")
def test_the_writer_answers_as_the_caller_and_with_the_headers_the_request_names(
    writer: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str, str, str, str, str]] = []

    def answer(found: Any, exchange: database_ledger.Exchange, slug: str, path: str) -> tuple[int, bytes]:
        worktree = str(exchange.headers.get("X-Ledger-Worktree"))
        seen.append(
            (database_ledger.principal(), worktree, slug, path, exchange.command, exchange.rfile.read().decode())
        )
        return 201, b'{"seen": 1}'

    monkeypatch.setattr(database_ledger, "answer", answer)
    asked = {
        "slug": "sw",
        "path": "operations",
        "payload": {"ops": []},
        "principal": "eng@ab-1",
        "headers": {"X-Ledger-Worktree": "%2Fcaller%2Ftree"},
    }
    with database_ledger.acting("eng@ab-2"):
        reply = raw(writer, json.dumps(asked).encode())
    assert reply == b'201\n{"seen": 1}'
    assert seen == [("eng@ab-1", "%2Fcaller%2Ftree", "sw", "operations", "POST", '{"ops": []}')]


@pytest.mark.feature("ledger-warm-writer")
def test_ask_sends_the_caller_name_and_the_in_process_headers(
    memory_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(memory_path)
    where = memory_path / "r.sock"
    for path, payload, headers in (
        ("operations", {"ops": []}, {"X-Ledger-Worktree": quote(os.getcwd())}),
        ("tasks", None, {}),
    ):
        thread, heard = recording(where, b'201\n{"ok": 1}')
        with database_ledger.acting("eng@ab-1"):
            answered = warm_writer.ask(warm_writer.connected(10.0, where), "sw", path, payload)
        thread.join(10.0)
        where.unlink()
        assert answered == (201, b'{"ok": 1}')
        assert json.loads(heard[0]) == {
            "slug": "sw",
            "path": path,
            "payload": payload,
            "principal": "eng@ab-1",
            "headers": headers,
        }


@pytest.mark.feature("ledger-warm-writer")
@pytest.mark.parametrize("reply", [b"", b"200"], ids=["nothing", "no-status-line"])
def test_ask_raises_when_the_writer_closes_without_an_answer(memory_path: Path, reply: bytes) -> None:
    where = memory_path / "r.sock"
    thread, _ = recording(where, reply)
    with pytest.raises(EOFError, match="the ledger writer closed with no answer"):
        warm_writer.ask(warm_writer.connected(10.0, where), "sw", "tasks", None)
    thread.join(10.0)


@pytest.mark.feature("ledger-warm-writer")
def test_a_connection_fails_where_no_writer_listens_and_carries_its_timeout(memory_path: Path, writer: Path) -> None:
    stale = memory_path / "stale.sock"
    left = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    left.bind(str(stale))
    left.close()
    with pytest.raises(FileNotFoundError):
        warm_writer.connected(10.0, memory_path / "none.sock")
    with pytest.raises(ConnectionRefusedError):
        warm_writer.connected(10.0, stale)
    with warm_writer.connected(2.5, writer) as sock:
        assert sock.gettimeout() == 2.5


@pytest.mark.feature("ledger-warm-writer")
def test_the_writer_socket_is_owner_only_in_the_agentihooks_home(
    memory_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", memory_path)
    still(monkeypatch)
    previous = os.umask(0o022)
    stopped = threading.Event()
    served = serving(warm_writer.socket_path(), stopped)
    try:
        with database_ledger.acting(""):
            answered = warm_writer.ask(warm_writer.connected(10.0), "warm-missing", "tasks", None)
            assert answered == in_process("warm-missing", "tasks")
        assert answered[0] == 404
        assert os.umask(0o022) == 0o022
        mode = (memory_path / "ledger-writer.sock").stat().st_mode
        assert stat.S_ISSOCK(mode)
        assert stat.S_IMODE(mode) == 0o600
    finally:
        os.umask(previous)
        stopped.set()
    served.join(10.0)
    assert not served.is_alive()
    assert not (memory_path / "ledger-writer.sock").exists()


@pytest.mark.feature("ledger-warm-writer")
def test_a_second_writer_on_a_live_socket_returns_one_and_leaves_the_first_answering(
    writer: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert warm_writer.serve(writer, threading.Event(), 0.05) == 1
    assert f"a ledger writer already answers on {writer}" in capsys.readouterr().err
    with database_ledger.acting(""):
        assert over_socket(writer, "warm-missing", "tasks")[0] == 404


@pytest.mark.feature("ledger-warm-writer")
def test_a_stale_socket_file_is_replaced_by_a_new_writer(memory_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    still(monkeypatch)
    where = memory_path / "w.sock"
    left = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    left.bind(str(where))
    left.close()
    stopped = threading.Event()
    served = serving(where, stopped)
    try:
        with database_ledger.acting(""):
            assert over_socket(where, "warm-missing", "tasks")[0] == 404
    finally:
        stopped.set()
    served.join(10.0)
    assert not served.is_alive()


@pytest.mark.feature("ledger-warm-writer")
def test_a_live_writer_too_busy_to_accept_the_probe_keeps_its_socket(
    memory_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    where = memory_path / "w.sock"
    left = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    left.bind(str(where))
    left.close()
    probes: list[tuple[float, Path | None]] = []

    def busy(timeout: float, at: Path | None = None) -> socket.socket:
        probes.append((timeout, at))
        raise TimeoutError("timed out")

    monkeypatch.setattr(warm_writer, "connected", busy)
    assert warm_writer.serve(where, threading.Event(), 0.05) == 1
    assert probes == [(warm_writer.PROBE_S, where)]
    assert where.exists()
    assert f"a ledger writer already answers on {where}" in capsys.readouterr().err


@pytest.mark.feature("ledger-warm-writer")
def test_a_stalled_client_neither_holds_other_requests_nor_keeps_the_writer_from_returning(
    memory_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    still(monkeypatch)
    monkeypatch.setattr(warm_writer.Answering, "timeout", 0.2)
    where = memory_path / "w.sock"
    stopped = threading.Event()
    served = serving(where, stopped)
    stalled = warm_writer.connected(10.0, where)
    try:
        with database_ledger.acting(""):
            assert over_socket(where, "warm-missing", "tasks")[0] == 404
        assert stalled.recv(1) == b""
    finally:
        stalled.close()
        stopped.set()
    served.join(10.0)
    assert not served.is_alive()


@pytest.mark.feature("ledger-warm-writer")
def test_a_writer_whose_socket_file_was_removed_still_returns_cleanly(
    memory_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    still(monkeypatch)
    where = memory_path / "w.sock"
    stopped = threading.Event()
    codes: list[int] = []
    served = threading.Thread(target=lambda: codes.append(warm_writer.serve(where, stopped, 0.05)), daemon=True)
    served.start()
    deadline = time.monotonic() + 10.0
    while not where.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        with database_ledger.acting(""):
            assert over_socket(where, "warm-missing", "tasks")[0] == 404
        where.unlink()
    finally:
        stopped.set()
    served.join(10.0)
    assert codes == [0]


@pytest.mark.feature("ledger-warm-writer")
def test_a_connection_that_sends_nothing_gets_no_reply_and_no_error(
    writer: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert raw(writer, b"") == b""
    with database_ledger.acting(""):
        assert over_socket(writer, "warm-missing", "tasks")[0] == 404
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.feature("ledger-warm-writer")
def test_the_writer_finishes_a_request_in_flight_then_returns_once_the_checkout_moves(
    memory_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = ["one"]

    def revision(root: Path | str) -> str:
        assert root == warm_writer.ROOT
        return checkout[0]

    entered, release = threading.Event(), threading.Event()

    def replied(slug: str, path: str, payload: Any, extra: Any) -> tuple[Any, int, bytes]:
        entered.set()
        release.wait(10.0)
        return None, 200, b'{"late": 1}'

    monkeypatch.setattr(warm_writer, "revision", revision)
    monkeypatch.setattr(ledger_server, "scheduled_wait", paced)
    monkeypatch.setattr(database_ledger, "replied", replied)
    where = memory_path / "w.sock"
    stopped, returned = threading.Event(), threading.Event()
    codes: list[int] = []

    def serve() -> None:
        codes.append(warm_writer.serve(where, stopped, 0.05))
        returned.set()

    threading.Thread(target=serve, daemon=True).start()
    answers: list[tuple[int, bytes]] = []
    deadline = time.monotonic() + 10.0
    while not where.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    asking = threading.Thread(target=lambda: answers.append(over_socket(where, "sw", "tasks")), daemon=True)
    asking.start()
    try:
        assert entered.wait(10.0)
        checkout[0] = "two"
        assert not returned.wait(0.3)
        release.set()
        assert returned.wait(10.0)
    finally:
        release.set()
        stopped.set()
    asking.join(10.0)
    assert answers == [(200, b'{"late": 1}')]
    assert codes == [0]
    assert not where.exists()
    assert f"the ledger writer {os.getpid()} restarts: the checkout moved from one to two" in capsys.readouterr().err


@pytest.mark.feature("ledger-warm-writer")
@pytest.mark.parametrize(
    ("current", "moved"), [("", False), ("one", False), ("two", True)], ids=["none", "same", "new"]
)
def test_the_checkout_moves_only_to_a_new_revision(monkeypatch: pytest.MonkeyPatch, current: str, moved: bool) -> None:
    monkeypatch.setattr(warm_writer, "revision", lambda root: current)
    assert warm_writer.moved("one") is moved


@pytest.mark.feature("ledger-warm-writer")
def test_the_writer_command_serves_the_home_socket_until_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[tuple[Path, threading.Event, list[str]]] = []

    def serve(where: Path, stopped: threading.Event) -> int:
        served.append((where, stopped, list(sys.argv)))
        return 0

    monkeypatch.setattr(warm_writer, "serve", serve)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert run(["writer", "serve"]) == 0
        handler: Any = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)
    [(where, stopped, argv)] = served
    assert where == warm_writer.socket_path()
    assert stopped.is_set()
    assert argv == ["agentihooks ledger writer", "serve"]
