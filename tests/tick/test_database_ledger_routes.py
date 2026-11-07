"""The in process ledger transport pinned to the exact statuses, bodies, headers and clients the server path gives."""

import io
import logging
import urllib.error
from email.message import Message
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm_ledger import ledger, ledger_route
from scripts.swarm_ledger.api import task_list
from scripts.tick.adapters import database_ledger

pytestmark = [pytest.mark.unit]


def _found(exists: bool = True) -> Any:
    return SimpleNamespace(repository=SimpleNamespace(exists=lambda slug: exists))


def _exchange(command: str = "GET") -> database_ledger.Exchange:
    return database_ledger.Exchange(command, "/api/v1/ledgers/sw/metadata", Message(), io.BytesIO(b""))


def _routes(monkeypatch: pytest.MonkeyPatch, **handlers: Any) -> list[tuple[str, tuple[Any, ...]]]:
    seen: list[tuple[str, tuple[Any, ...]]] = []

    def record(name: str) -> Any:
        def call(*args: Any) -> Any:
            seen.append((name, args))
            outcome = handlers[name]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome(*args) if callable(outcome) else outcome

        return call

    monkeypatch.setattr(
        database_ledger,
        "routes",
        SimpleNamespace(ledger_get=record("ledger_get"), ledger_operation=record("ledger_operation")),
    )
    return seen


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_missing_ledger_answers_the_server_404_body() -> None:
    assert database_ledger.answer(_found(False), _exchange(), "gone", "metadata") == (
        404,
        b'{"error": {"code": "ledger_missing", "message": "No such ledger"}}',
    )


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_storage_failure_answers_the_server_500_body_and_logs_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _routes(monkeypatch, ledger_get=OSError("disk"))
    with caplog.at_level(logging.ERROR, logger="scripts.tick.adapters.database_ledger"):
        answered = database_ledger.answer(_found(), _exchange(), "sw", "metadata")
    assert answered == (
        500,
        b'{"error": {"code": "storage_error", "message": "Resource could not be read or written"}}',
    )
    assert [(record.name, record.getMessage()) for record in caplog.records] == [
        ("scripts.tick.adapters.database_ledger", "Ledger API storage failure")
    ]


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_read_is_bounded_and_encoded_as_ascii_json(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _routes(monkeypatch, ledger_get={"data": "café"})
    bounded: list[object] = []
    monkeypatch.setattr(database_ledger, "resources", SimpleNamespace(bounded=bounded.append))
    exchange, found = _exchange(), _found()
    assert database_ledger.answer(found, exchange, "sw", "tasks") == (200, b'{"data": "caf\\u00e9"}')
    assert bounded == [{"data": "café"}]
    assert seen == [("ledger_get", (exchange, found, "sw", "tasks"))]


@pytest.mark.feature("swarm-tick-database-ledger")
def test_an_export_reply_is_not_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    _routes(monkeypatch, ledger_operation={"data": {}})
    bounded: list[object] = []
    monkeypatch.setattr(database_ledger, "resources", SimpleNamespace(bounded=bounded.append))
    assert database_ledger.answer(_found(), _exchange("POST"), "sw", "export") == (200, b'{"data": {}}')
    assert bounded == []


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_write_runs_as_the_service_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _routes(monkeypatch, ledger_operation={"applied": []})
    monkeypatch.setattr(database_ledger, "resources", SimpleNamespace(bounded=lambda reply: reply))
    database_ledger.answer(_found(), _exchange("POST"), "sw", "operations")
    assert seen[0][0] == "ledger_operation"
    assert seen[0][1][2:] == ("sw", "operations", "")


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_streamed_reply_and_a_task_page_answer_as_the_server_sends_them(monkeypatch: pytest.MonkeyPatch) -> None:
    exchange = _exchange("POST")

    def stream(sent: database_ledger.Exchange, *rest: object) -> None:
        sent.send_export(b'{"generation":3}')

    _routes(monkeypatch, ledger_operation=stream, ledger_get=task_list.Page({"data": [], "next_cursor": None}))
    assert database_ledger.answer(_found(), exchange, "sw", "export") == (200, b'{"generation":3}')
    status, body = database_ledger.answer(_found(), _exchange(), "sw", "tasks")
    assert (status, body) == (200, task_list.Page({"data": [], "next_cursor": None}).text().encode())


@pytest.mark.feature("swarm-tick-database-ledger")
def test_an_exchange_keeps_what_the_route_code_sends() -> None:
    exchange = _exchange()
    exchange.send(409, '{"error": {}}', "application/json")
    assert (exchange.status, exchange.reply) == (409, b'{"error": {}}')
    exchange.status = 500
    exchange.send_export(b"{}")
    assert (exchange.status, exchange.reply) == (200, b"{}")


def _answered(monkeypatch: pytest.MonkeyPatch, status: int, body: bytes) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    services = object()
    monkeypatch.setattr(database_ledger, "services", lambda: services)

    def answer(found: object, exchange: database_ledger.Exchange, slug: str, path: str) -> tuple[int, bytes]:
        calls.append((found is services, exchange, slug, path))
        return status, body

    monkeypatch.setattr(database_ledger, "answer", answer)
    monkeypatch.setattr(database_ledger, "writer_header", lambda: {"X-Ledger-Worktree": "%2Fw"})
    return calls


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_read_request_hands_the_route_code_a_bodiless_get(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _answered(monkeypatch, 200, b'{"data": 1}')
    assert database_ledger.request("a/b", "tasks?limit=1") == {"data": 1}
    [(services, exchange, slug, path)] = calls
    assert (services, slug, path) == (True, "a/b", "tasks")
    assert (exchange.command, exchange.path) == ("GET", "/api/v1/ledgers/a%2Fb/tasks?limit=1")
    assert list(exchange.headers.items()) == [("Content-Type", "application/json"), ("Content-Length", "0")]
    assert exchange.rfile.read() == b""


@pytest.mark.feature("swarm-tick-database-ledger")
def test_an_operations_request_posts_its_body_with_the_writer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _answered(monkeypatch, 200, b"{}")
    database_ledger.request("sw", "operations", {"ops": []})
    exchange = calls[0][1]
    assert exchange.command == "POST"
    assert list(exchange.headers.items()) == [
        ("Content-Type", "application/json"),
        ("Content-Length", "11"),
        ("X-Ledger-Worktree", "%2Fw"),
    ]
    assert exchange.rfile.read() == b'{"ops": []}'
    database_ledger.request("sw", "export", {})
    assert "X-Ledger-Worktree" not in calls[1][1].headers


@pytest.mark.feature("swarm-tick-database-ledger")
def test_an_empty_path_reads_the_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _answered(monkeypatch, 200, b"{}")
    database_ledger.request("sw", "")
    assert calls[0][3] == "metadata"


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_refused_request_raises_the_server_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _answered(monkeypatch, 400, b'{"error": {"code": "schema_invalid"}}')
    with pytest.raises(urllib.error.HTTPError) as refused:
        database_ledger.request("sw", "metadata")
    assert (refused.value.url, refused.value.code, refused.value.msg) == (
        "/api/v1/ledgers/sw/metadata",
        400,
        "ledger refused",
    )
    assert refused.value.hdrs["Content-Type"] == "application/json"
    assert refused.value.read() == b'{"error": {"code": "schema_invalid"}}'


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_reply_below_400_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    _answered(monkeypatch, 399, b'{"moved": true}')
    assert database_ledger.request("sw", "metadata") == {"moved": True}


@pytest.mark.feature("swarm-tick-database-ledger")
def test_the_database_client_carries_the_base_timeout_and_in_process_transport() -> None:
    built = database_ledger.client("http://ledger", 2.5)
    assert (built.base, built.credentials, built.timeout) == ("http://ledger", {}, 2.5)
    assert built.transport is database_ledger.request


@pytest.mark.feature("swarm-tick-database-ledger")
def test_the_ledger_client_is_the_database_client_inside_a_database_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger, "base", lambda slug: f"base:{slug}")
    services: list[bool] = []
    monkeypatch.setattr(ledger, "credentials", lambda slug, service: services.append(service) or {"X": slug})
    with database_ledger.scoped({database_ledger.postgres.URL: "postgresql://ledger@shared/ledger"}):
        inside = ledger.client("sw", timeout=3.0)
    outside = ledger.client("sw")
    assert (inside.base, inside.credentials, inside.timeout) == ("base:sw", {}, 3.0)
    assert inside.transport is database_ledger.request
    assert (outside.base, outside.credentials, outside.transport) == ("base:sw", {"X": "sw"}, None)
    assert services == [False]


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_database_call_sends_its_service_flag_and_exits_as_the_server_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[Any, ...]] = []
    monkeypatch.setattr(ledger, "request", lambda *args: sent.append(args) or {"rev": 1})
    assert ledger.database_call("sw", [{"op": "add"}], True) == {"rev": 1}
    assert sent == [("sw", [{"op": "add"}], True)]

    def refuse(*args: object) -> dict[str, Any]:
        raise urllib.error.HTTPError("u", 409, "m", Message(), io.BytesIO(b"busy"))

    monkeypatch.setattr(ledger, "request", refuse)
    with pytest.raises(SystemExit, match="^server refused: 409 busy$"):
        ledger.database_call("sw", None, False)


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_tick_routed_call_keeps_its_own_ledger_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger_route, "configured", lambda store, slug: "http://hive-ledger")

    @ledger_route.tick
    def probe(store: object, slug: str) -> str:
        return ledger_route.url(slug)

    assert probe(object(), "sw") == "http://hive-ledger"
