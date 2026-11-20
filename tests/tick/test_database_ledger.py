"""The tick's ledger traffic served in process over the shared repository by the ledger API route code, with no
ledger server running; which repository the database URL selects is the storage module's seam."""

import importlib
import io
import json
import sys
import urllib.error
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from scripts.swarm import lease, timing
from scripts.swarm import ledger_client as ledger_client_module
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import api_service, ledger_server
from scripts.swarm_ledger.api.client import kept_exports
from scripts.tick.adapters import database_ledger
from scripts.tick.adapters.ledger import ClientLedger, resource_client
from scripts.tick.deadline import Deadline

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
cli_ledger: Any = importlib.import_module("ledger")
core: Any = importlib.import_module("ledger_core")
new_ledger: Any = importlib.import_module("new_ledger")

pytestmark = pytest.mark.unit

SLUG = "database-tick"
DOWN = "http://127.0.0.1:9"


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch, memory_path: Path) -> Iterator[list[tuple[str, int]]]:
    """A ledger on disk, its ledger server address pointing at a closed port, the epoch fence recorded."""
    monkeypatch.setattr(core, "LEDGER_DIR", memory_path)
    monkeypatch.setenv("LEDGER_DIR", str(memory_path))
    content = {"title": "Database tick", "overview": "o", "phases": [{"title": "One", "description": "d"}]}
    assert new_ledger.create(SLUG, content)
    fenced: list[tuple[str, int]] = []
    monkeypatch.setattr(cli_ledger, "BASE", DOWN)
    monkeypatch.setattr(api_service, "relay_to_inbox", lambda slug, state: None)
    monkeypatch.setattr(api_service, "doctor_phrase", lambda slug, state: None)
    monkeypatch.setattr(api_service, "deliver_alerts", lambda slug, state: None)
    monkeypatch.setattr(ledger_server.authority, "fence", lambda slug, epoch: fenced.append((slug, epoch)))
    with (
        patch("hooks._redis.get_redis", return_value=None),
        patch("scripts.gates.talk.Budget._marks", return_value=None),
    ):
        yield fenced


def _comment(text: str) -> dict[str, object]:
    return {"op": "add", "id": f"add-{abs(hash(text))}", "by": "swarm", "thread": "phases/p1/comments", "text": text}


def _port() -> ClientLedger:
    return ClientLedger(SLUG, resource_client(SLUG))


def _deadline() -> Deadline:
    return Deadline(30.0, lambda: 0.0)


@pytest.mark.feature("swarm-tick-database-ledger")
def test_the_port_exports_the_ledger_with_the_ledger_server_down(stored: list[tuple[str, int]]) -> None:
    with kept_exports():
        exported = _port().export(deadline=_deadline())
    phases: Any = exported["phases"]
    assert exported["title"] == "Database tick"
    assert [phase["id"] for phase in phases] == ["p1"]


@pytest.mark.feature("swarm-tick-database-ledger")
def test_the_port_writes_with_the_controller_epoch_with_the_ledger_server_down(
    stored: list[tuple[str, int]],
) -> None:
    with lease.fencing(7):
        reply = _port().mutate([_comment("Pass landed")], deadline=_deadline())
    assert reply["rejected"] == []
    assert stored == [(SLUG, 7), (SLUG, 7)]
    assert [comment["text"] for comment in core.sync(SLUG)[0]["phases"][0]["comments"]] == ["Pass landed"]


@pytest.mark.feature("swarm-tick-database-ledger")
def test_a_stale_controller_write_is_refused_as_the_server_refuses_it(
    stored: list[tuple[str, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def stale(slug: str, epoch: int) -> None:
        raise SwarmError(f"controller epoch {epoch} is stale")

    monkeypatch.setattr(ledger_server.authority, "fence", stale)
    with lease.fencing(3), pytest.raises(urllib.error.HTTPError) as refused:
        _port().mutate([_comment("Old pass")], deadline=_deadline())
    assert refused.value.code == 409
    assert b'"stale_controller"' in refused.value.read()
    assert core.sync(SLUG)[0]["phases"][0]["comments"] == []


@pytest.mark.feature("swarm-tick-database-ledger")
def test_the_pass_ledger_reads_and_writes_with_the_ledger_server_down(stored: list[tuple[str, int]]) -> None:
    client = LedgerClient(service=True)
    client.add_task(SLUG, {"task": "t1", "title": "Task one", "lane": "eng", "phase": "p1"}, "swarm")
    client.update_task(SLUG, "t1", {"state": "claimed"})
    state = ledger_client_module.pass_state(client, SLUG)
    task = client.task(SLUG, "t1")
    assert [row["id"] for row in state["tasks"]] == ["t1"]
    assert task is not None
    assert task["state"] == "claimed"


def ledger_calls(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith('{"event"')]
    return [row for row in rows if row["event"] == "swarm_tick_ledger_call"]


@pytest.mark.feature("swarm-tick-ledger-call-timing")
def test_each_ledger_request_of_a_tick_pass_journals_its_route_outcome_and_wall_time(
    stored: list[tuple[str, int]], capsys: pytest.CaptureFixture[str]
) -> None:
    client = LedgerClient(service=True)
    client.add_task(SLUG, {"task": "t1", "title": "Task one", "lane": "eng", "phase": "p1"}, "swarm")
    client.tasks(SLUG)
    assert ledger_calls(capsys) == []
    passing = timing.PASS.set({"pass_id": "p1"})
    try:
        with timing.tick("sw"):
            client.tasks(SLUG)
            with pytest.raises(urllib.error.HTTPError):
                database_ledger.client(cli_ledger.base(SLUG), 5).request("no-such-ledger", "metadata")
    finally:
        timing.PASS.reset(passing)
    calls = ledger_calls(capsys)
    assert [(call["ledger"], call["method"], call["path"], call["outcome"]) for call in calls] == [
        (SLUG, "GET", "tasks", "success"),
        ("no-such-ledger", "GET", "metadata", "error"),
    ]
    assert all(call["slug"] == "sw" and call["pass_id"] == "p1" and call["wall_s"] >= 0 for call in calls)


def naming(agent: str | None) -> database_ledger.Exchange:
    """An in process operations request that names ``agent`` in its agent header, or names none."""
    headers = Message()
    if agent is not None:
        headers["X-Ledger-Agent"] = agent
    return database_ledger.Exchange("POST", f"/api/v1/ledgers/{SLUG}/operations", headers, io.BytesIO(b""))


def test_a_process_acting_as_one_agent_refuses_a_request_naming_another() -> None:
    exchange = naming("eng@b")
    with database_ledger.acting("eng@a"):
        assert exchange.refused() is True
    assert (exchange.principal, exchange.status) == ("eng@a", 403)
    assert json.loads(exchange.reply) == {
        "error": {"code": "forbidden", "message": "this process acts as eng@a, not eng@b"}
    }


@pytest.mark.parametrize(("acting", "agent"), [("eng@a", "eng@a"), ("eng@a", None), ("", "eng@b")])
def test_a_request_naming_the_acting_agent_or_no_agent_or_sent_as_the_service_is_admitted(
    acting: str, agent: str | None
) -> None:
    exchange = naming(agent)
    with database_ledger.acting(acting):
        assert exchange.refused() is False
    assert (exchange.principal, exchange.status, exchange.reply) == (acting, 200, b"")


def test_an_exchange_the_route_refuses_raises_its_status_body_and_request(stored: list[tuple[str, int]]) -> None:
    token = api_service.services().repository.token(SLUG)
    assert token is not None
    path = f"/api/v1/ledgers/{SLUG}/tasks?limit=many"
    with pytest.raises(urllib.error.HTTPError) as refused:
        database_ledger.exchanged("GET", path, None, {"X-Ledger-Token": token})
    error = refused.value
    assert (error.code, error.filename, error.msg, error.hdrs["X-Ledger-Token"]) == (400, path, "ledger refused", token)
    assert json.loads(error.read()) == {"error": {"code": "schema_invalid", "message": "Limit must be an integer"}}


def test_an_exchange_the_route_answers_itself_returns_that_answer(stored: list[tuple[str, int]]) -> None:
    token = api_service.services().repository.token(SLUG)
    assert token is not None
    exported = database_ledger.exchanged("POST", f"/api/v1/ledgers/{SLUG}/export", {}, {"X-Ledger-Token": token})
    assert "data" in exported


def test_an_empty_upload_raises_the_route_status_body_and_request(stored: list[tuple[str, int]]) -> None:
    with pytest.raises(urllib.error.HTTPError) as refused:
        database_ledger.upload(SLUG, "media", b"", {"Content-Type": "application/octet-stream"})
    error = refused.value
    assert (error.code, error.filename, error.msg, error.hdrs["Content-Length"]) == (
        400,
        f"/api/v1/ledgers/{SLUG}/uploads/media",
        "ledger refused",
        "0",
    )
    assert json.loads(error.read())["error"]["code"] == "schema_invalid"
