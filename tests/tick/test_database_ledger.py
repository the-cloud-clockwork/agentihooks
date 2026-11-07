"""The tick's ledger traffic served in process from the shared ledger repository with no ledger server running."""

import sys
import urllib.error
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.swarm import lease
from scripts.swarm import ledger_client as ledger_client_module
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger, ledger_route, ledger_server
from scripts.swarm_ledger.api import local_client
from scripts.tick.adapters.ledger import ClientLedger, resource_client
from scripts.tick.deadline import Deadline

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger as cli_ledger  # noqa: E402
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.feature("swarm-tick-database-ledger")]

SLUG = "database-tick"
DOWN = "http://127.0.0.1:9"
DATABASE = "postgresql://ledger@shared/ledger"


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch, memory_path: Path) -> list[tuple[str, int]]:
    """A ledger on disk, its ledger server address pointing at a closed port, the epoch fence recorded."""
    monkeypatch.setattr(core, "LEDGER_DIR", memory_path)
    monkeypatch.setenv("LEDGER_DIR", str(memory_path))
    content = {"title": "Database tick", "overview": "o", "phases": [{"title": "One", "description": "d"}]}
    assert new_ledger.create(SLUG, content)
    fenced: list[tuple[str, int]] = []
    monkeypatch.setattr(ledger, "BASE", DOWN)
    monkeypatch.setattr(cli_ledger, "BASE", DOWN)
    monkeypatch.setattr(ledger_server, "relay_to_inbox", lambda slug, state: None)
    monkeypatch.setattr(ledger_server, "doctor_phrase", lambda slug, state: None)
    monkeypatch.setattr(ledger_server, "deliver_alerts", lambda slug, state: None)
    monkeypatch.setattr(ledger_server.authority, "fence", lambda slug, epoch: fenced.append((slug, epoch)))
    with (
        patch("hooks._redis.get_redis", return_value=None),
        patch("scripts.gates.talk.Budget._marks", return_value=None),
    ):
        yield fenced


def _serving() -> AbstractContextManager[None]:
    """Serve ledger calls in process while inside, as a tick pass does with the database URL set."""
    return local_client.scoped({local_client.postgres.URL: DATABASE})


def _comment(text: str) -> dict[str, object]:
    return {"op": "add", "id": f"add-{abs(hash(text))}", "by": "swarm", "thread": "phases/p1/comments", "text": text}


def _port() -> ClientLedger:
    return ClientLedger(SLUG, resource_client(SLUG, service=True))


def _deadline() -> Deadline:
    return Deadline(30.0, lambda: 0.0)


def test_a_tick_routed_call_serves_in_process_only_while_the_database_url_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SimpleNamespace(redis=SimpleNamespace(hget=lambda *names: None), key=lambda slug, name: f"{slug}:{name}")

    @ledger_route.tick
    def probe(store: object, slug: str) -> bool:
        return local_client.serving()

    monkeypatch.delenv(local_client.postgres.URL, raising=False)
    assert probe(store, SLUG) is False
    monkeypatch.setenv(local_client.postgres.URL, DATABASE)
    assert probe(store, SLUG) is True
    assert local_client.serving() is False


def test_the_port_exports_the_ledger_with_the_ledger_server_down(stored: list[tuple[str, int]]) -> None:
    with _serving():
        exported = _port().export(deadline=_deadline())
    assert exported["title"] == "Database tick"
    assert [phase["id"] for phase in exported["phases"]] == ["p1"]


def test_the_port_writes_with_the_controller_epoch_with_the_ledger_server_down(
    stored: list[tuple[str, int]],
) -> None:
    with _serving(), lease.fencing(7):
        reply = _port().mutate([_comment("Pass landed")], deadline=_deadline())
    assert reply["rejected"] == []
    assert stored == [(SLUG, 7), (SLUG, 7)]
    assert [comment["text"] for comment in core.sync(SLUG)[0]["phases"][0]["comments"]] == ["Pass landed"]


def test_a_stale_controller_write_is_refused_as_the_server_refuses_it(
    stored: list[tuple[str, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def stale(slug: str, epoch: int) -> None:
        raise SwarmError(f"controller epoch {epoch} is stale")

    monkeypatch.setattr(ledger_server.authority, "fence", stale)
    with _serving(), lease.fencing(3), pytest.raises(urllib.error.HTTPError) as refused:
        _port().mutate([_comment("Old pass")], deadline=_deadline())
    assert refused.value.code == 409
    assert b'"stale_controller"' in refused.value.read()
    assert core.sync(SLUG)[0]["phases"][0]["comments"] == []


def test_the_pass_ledger_reads_and_writes_with_the_ledger_server_down(stored: list[tuple[str, int]]) -> None:
    client = LedgerClient(service=True)
    with _serving():
        client.add_task(SLUG, {"task": "t1", "title": "Task one", "lane": "eng", "phase": "p1"}, "swarm")
        client.update_task(SLUG, "t1", {"state": "claimed"})
        state = ledger_client_module.pass_state(client, SLUG)
        task = client.task(SLUG, "t1")
    assert [row["id"] for row in state["tasks"]] == ["t1"]
    assert task["state"] == "claimed"


def test_outside_a_database_pass_the_port_still_calls_the_ledger_server(stored: list[tuple[str, int]]) -> None:
    with pytest.raises(OSError):
        _port().export(deadline=_deadline())
