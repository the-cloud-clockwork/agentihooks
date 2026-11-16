"""The in process ledger calls: each reads through the client of its own ledger, a queued write and a refused service
say why, and the in process exchange hands the route code the request it was given."""

import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import authority_context, chat_ledger, ledger, ledger_hook
from scripts.swarm_v2.hosted_ledger import HostedLedger
from scripts.tick.adapters import database_ledger

SLUG = "rig"


class Reader:
    """A ledger API client that records each call and answers it."""

    def __init__(self) -> None:
        self.asked: list[tuple[object, ...]] = []

    def request(self, slug: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.asked.append((slug, path, payload))
        return {"data": {"path": path}}

    def mutate(self, slug: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        self.asked.append((slug, ops))
        return {"applied": [op["id"] for op in ops]}


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> tuple[Reader, list[tuple[str, float]]]:
    """The client every ledger call gets, and the ledger and timeout each call built it for."""
    reader = Reader()
    built: list[tuple[str, float]] = []

    def client(slug: str, timeout: float = ledger.REQUEST_TIMEOUT) -> Reader:
        built.append((slug, timeout))
        return reader

    monkeypatch.setattr(ledger, "client", client)
    return reader, built


def test_an_export_reads_through_the_client_of_its_own_ledger(clients: tuple[Reader, list[tuple[str, float]]]) -> None:
    reader, built = clients
    assert ledger.export(SLUG) == {"path": "export"}
    assert (built, reader.asked) == ([(SLUG, ledger.EXPORT_TIMEOUT)], [(SLUG, "export", {})])


def test_a_resource_reads_through_the_client_of_its_own_ledger(clients: tuple[Reader, list[tuple[str, float]]]) -> None:
    reader, built = clients
    assert ledger.resource(SLUG, "tasks/t1") == {"path": "tasks/t1"}
    assert (built, reader.asked) == ([(SLUG, ledger.REQUEST_TIMEOUT)], [(SLUG, "tasks/t1", None)])


def test_a_replayed_write_goes_through_the_client_of_its_own_ledger(
    clients: tuple[Reader, list[tuple[str, float]]],
) -> None:
    reader, built = clients
    ops = [{"op": "add", "id": "o-1"}]
    assert ledger.replay(SLUG, ops, "eng@a", False) == {"applied": ["o-1"]}
    assert (built, reader.asked) == ([(SLUG, ledger.WRITE_TIMEOUT)], [(SLUG, ops)])


def test_a_queued_write_says_so_on_standard_error_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    appended: list[object] = []

    def holds(operation: str) -> bool:
        return False

    monkeypatch.setattr(ledger.outbox, "holds", holds)
    monkeypatch.setattr(ledger.outbox, "append", appended.append)
    assert ledger.queue(SLUG, [{"op": "add", "id": "o-1"}], True) == {"queued": "o-1"}
    assert len(appended) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        "",
        "the ledger database is not answering: queued locally, it replays when it answers\n",
    )


@pytest.fixture
def refusing(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The ledgers asked whether they refuse the swarm's service, each of which does."""
    asked: list[str] = []

    def refused(slug: str) -> bool:
        asked.append(slug)
        return True

    monkeypatch.setattr(authority_context, "service_refused", refused)
    return asked


def test_a_stop_gate_bypass_the_service_may_not_write_is_logged_for_its_ledger(
    monkeypatch: pytest.MonkeyPatch, refusing: list[str]
) -> None:
    logged: list[str] = []
    monkeypatch.setattr(ledger_hook, "log", logged.append)
    ledger_hook.post_bypass({"slug": SLUG, "name": "eng@a"}, 2)
    assert refusing == [SLUG]
    assert logged == [f"bypass not recorded: {authority_context.REMOTE_SERVICE}"]


def test_a_service_call_asks_its_own_ledger_whether_the_service_is_refused(refusing: list[str]) -> None:
    with pytest.raises(SwarmError) as stopped:
        LedgerClient(service=True)._call(SLUG)
    assert refusing == [SLUG]
    assert str(stopped.value) == f"ledger {SLUG}: {authority_context.REMOTE_SERVICE}"


def test_the_chat_command_names_a_ledger_database_that_does_not_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(slug: str, ops: list[dict[str, Any]] | None = None, timeout: float | None = None) -> dict[str, Any]:
        raise OSError("connection refused")

    monkeypatch.setattr(cast(Any, chat_ledger).ledger, "request", gone)
    monkeypatch.setattr(sys, "argv", ["chat_ledger.py", SLUG, "--as", "eng@a", "--reply-to", "o-0", "Hello"])
    with pytest.raises(SystemExit) as stopped:
        chat_ledger.main()
    assert stopped.value.code == "the ledger database is not answering: connection refused"


def test_the_hosted_ledger_client_keeps_the_hosted_base_and_sends_no_credentials() -> None:
    built = HostedLedger("https://ledger.test", cast(Any, None)).client()
    assert (built.base, built.credentials, built.timeout) == ("https://ledger.test", {}, ledger.REQUEST_TIMEOUT)


def test_an_upload_hands_the_route_its_request_path_and_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[database_ledger.Exchange] = []

    def answer(found: object, exchange: database_ledger.Exchange, slug: str, path: str) -> tuple[int, bytes]:
        seen.append(exchange)
        return 200, b'{"id": "stored"}'

    def services() -> None:
        return None

    monkeypatch.setattr(database_ledger, "services", services)
    monkeypatch.setattr(database_ledger, "answer", answer)
    assert database_ledger.upload(SLUG, "media", b"image", {"X-Ledger-Agent": "eng@a"}) == {"id": "stored"}
    [exchange] = seen
    assert (exchange.command, exchange.path, exchange.rfile.read(), exchange.headers["Content-Length"]) == (
        "POST",
        f"/api/v1/ledgers/{SLUG}/uploads/media",
        b"image",
        "5",
    )


def test_an_exchange_without_a_payload_hands_the_route_an_empty_json_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[database_ledger.Exchange] = []

    def settled(exchange: database_ledger.Exchange, path: str, route: object) -> tuple[int, bytes]:
        seen.append(exchange)
        return 200, b'{"data": 1}'

    def services() -> SimpleNamespace:
        return SimpleNamespace(policy=SimpleNamespace(allowed_hosts={"ledger.test"}))

    monkeypatch.setattr(database_ledger, "services", services)
    monkeypatch.setattr(database_ledger, "settled", settled)
    path = f"/api/v1/ledgers/{SLUG}/tasks"
    assert database_ledger.exchanged("GET", path, None, {"X-Ledger-Token": "token"}) == {"data": 1}
    [exchange] = seen
    assert (exchange.command, exchange.path, exchange.rfile.read()) == ("GET", path, b"")
    assert [exchange.headers[name] for name in ("Host", "Content-Type", "Content-Length", "X-Ledger-Token")] == [
        "ledger.test",
        "application/json",
        "0",
        "token",
    ]
