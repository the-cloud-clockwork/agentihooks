"""A tick pass whose ledger server is slow or down runs from the last good view this host stored."""

import io
import urllib.error
from contextlib import ExitStack
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import cli, ledger_client
from scripts.swarm.ledger_client import OFFLINE, LedgerClient, LedgerUnreachable
from scripts.swarm_ledger.api import client as api
from scripts.tick import view
from scripts.tick.adapters.ledger import resource_client

STATE = {
    "tasks": [{"id": "t1", "state": "open"}, {"id": "t2", "state": "claimed"}],
    "_meta": {"rev": 4, "events": []},
    "phases": [{"id": "p1", "done": False}],
}


@pytest.fixture
def stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[dict[str, Any]], bool]]:
    """A ledger module that queues every write and refuses calls, with the view of ``sw`` stored at generation 7."""
    from scripts.scheduler import service

    queued: list[tuple[str, list[dict[str, Any]], bool]] = []

    def queue(slug: str, ops: list[dict[str, Any]], service: bool) -> dict[str, Any]:
        queued.append((slug, ops, service))
        return {"queued": ops[0]["id"]}

    def call(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("an offline pass never calls the server")

    ledger = SimpleNamespace(
        base=lambda slug: "http://ledger",
        credentials=lambda slug, service: {},
        REQUEST_TIMEOUT=4,
        queue=queue,
        call=call,
        Missing=SystemExit,
    )
    monkeypatch.setattr(ledger_client, "_ledger", lambda: ledger)
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    api.write_export_file(view.view_file("http://ledger", "sw", tmp_path), 7, STATE)
    return queued


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_an_offline_pass_holds_the_stored_view_and_queues_its_writes_onto_it(stored: list) -> None:
    with view.holding():
        held = view.hold_stored("sw", True)
        assert held is not None and held.generation == 7
        assert view.document("sw") == STATE
        token = OFFLINE.set(True)
        try:
            task = LedgerClient().update_task("sw", "t1", {"state": "claimed", "claimed_by": "e1"})
        finally:
            OFFLINE.reset(token)
        assert task == {"id": "t1", "state": "claimed", "claimed_by": "e1"}
        [(slug, ops, _)] = stored
        assert (slug, ops[0]["op"], ops[0]["item"]) == ("sw", "task_update", "tasks/t1")
        current = view.current()
        assert current is not None and current.generation == 7
        assert current.read("tasks", "t1") == {"id": "t1", "state": "claimed", "claimed_by": "e1"}
        assert current.read("tasks", "t2") == {"id": "t2", "state": "claimed"}
        assert view.document("sw")["_meta"] == STATE["_meta"]


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_an_offline_pass_refuses_fresh_reads_at_once(stored: list) -> None:
    client = LedgerClient()
    token = OFFLINE.set(True)
    try:
        for read in (client.state, client.metadata, client.tasks):
            with pytest.raises(LedgerUnreachable, match="last good view"):
                read("sw")
        with pytest.raises(LedgerUnreachable, match="last good view"):
            resource_client("sw")(5.0)
    finally:
        OFFLINE.reset(token)
    assert stored == []


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_a_host_without_a_stored_view_holds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.scheduler import service

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(base=lambda slug: "http://ledger"))
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    with view.holding():
        assert view.hold_stored("sw", False) is None
        assert view.current() is None
        assert view.document("sw") is None


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_queued_writes_without_a_held_view_patch_nothing() -> None:
    with view.holding():
        assert view.queued([{"op": "task_update", "item": "tasks/t1", "fields": {"state": "claimed"}}]) == {}


def refusal(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://ledger/export", code, "refused", Message(), io.BytesIO(b"{}"))


@pytest.mark.feature("swarm-tick-cached-ledger")
@pytest.mark.parametrize(
    "failure",
    [urllib.error.URLError("refused"), TimeoutError("timed out"), refusal(503)],
    ids=["stopped", "slow", "failing"],
)
def test_a_slow_or_down_server_leaves_the_pass_offline_on_the_stored_view(
    failure: OSError, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts: list[float | None] = []
    stored = view.LedgerView(7, STATE)

    def start(slug: str, service: bool = False, timeout: float | None = None) -> None:
        timeouts.append(timeout)
        raise failure

    monkeypatch.setattr(cli.ledger_view, "start", start)
    monkeypatch.setattr(cli.ledger_view, "hold_stored", lambda slug, service: stored)
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))
        assert OFFLINE.get() is True
    assert OFFLINE.get() is False
    assert timeouts == [ledger_client.SNAPSHOT_TIMEOUT_S]


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_a_down_server_without_a_stored_view_ends_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    def start(slug: str, service: bool = False, timeout: float | None = None) -> None:
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(cli.ledger_view, "start", start)
    monkeypatch.setattr(cli.ledger_view, "hold_stored", lambda slug, service: None)
    with ExitStack() as beating, pytest.raises(LedgerUnreachable, match="no stored view"):
        cli._hold_view(beating, "sw", LedgerClient())
    assert OFFLINE.get() is False


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_a_refusing_server_still_ends_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    def start(slug: str, service: bool = False, timeout: float | None = None) -> None:
        raise refusal(401)

    monkeypatch.setattr(cli.ledger_view, "start", start)
    with ExitStack() as beating, pytest.raises(urllib.error.HTTPError):
        cli._hold_view(beating, "sw", LedgerClient())
    assert OFFLINE.get() is False
