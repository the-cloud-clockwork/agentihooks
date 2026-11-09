import json
import time
import urllib.error
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm_ledger import ledger_link, ledger_route, storage
from scripts.swarm_ledger.api import client as api_client
from scripts.swarm_ledger.api import in_process
from scripts.swarm_ledger.api.client import ResourceClient, failure
from scripts.swarm_ledger.repository.postgres import URL
from scripts.tick.adapters import ledger as ledger_adapter
from scripts.tick.deadline import Deadline

pytestmark = pytest.mark.unit

DATABASE = {URL: "postgresql://shared-ledger-database"}
DOWN = "http://127.0.0.1:9"
CONTENT = {
    "title": "In process ledger",
    "overview": "The tick reads and writes the shared database.",
    "sources": [],
    "phases": [{"title": "Database", "description": "Every tick request runs in process."}],
    "tasks": [{"title": "Database task", "phase": "p1", "lane": "eng"}],
}


@pytest.fixture
def slug() -> str:
    name = f"inproc-{uuid.uuid4().hex[:10]}"
    assert storage.default().create(name, CONTENT)
    return name


@pytest.fixture
def server_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def down(request: object, timeout: float) -> object:
        calls.append(request)
        raise OSError("ledger server down")

    monkeypatch.setattr(ledger_link, "urlopen", down)
    return calls


def _chat(text: str) -> dict[str, Any]:
    return {"op": "add", "id": f"chat-{uuid.uuid4().hex[:10]}", "thread": "chat", "text": text}


@pytest.mark.feature("tick-ledger-in-process")
def test_connect_runs_in_process_only_inside_a_database_scope() -> None:
    assert type(in_process.connect(DOWN, {})) is ResourceClient
    with in_process.scoped(DATABASE):
        assert isinstance(in_process.connect(DOWN, {}, 3.0), in_process.InProcessClient)
        assert in_process.connect(DOWN, {}, 3.0).timeout == 3.0
        with in_process.scoped({}):
            assert type(in_process.connect(DOWN, {})) is ResourceClient
    assert not in_process.active()


@pytest.mark.feature("tick-ledger-in-process")
def test_export_and_mutate_run_in_process_with_the_ledger_server_down(slug: str, server_calls: list[object]) -> None:
    with in_process.scoped(DATABASE):
        client = in_process.connect(DOWN, {})
        reply = client.mutate(slug, [_chat("written by the tick in process")])
        state = client.export(slug, {})
    assert reply["applied"] and not reply["rejected"]
    assert "written by the tick in process" in json.dumps(state)
    assert [task["title"] for task in state["tasks"]] == ["Database task"]
    assert server_calls == []


@pytest.mark.feature("tick-ledger-in-process")
def test_a_refusal_in_process_raises_the_same_http_error(server_calls: list[object]) -> None:
    with in_process.scoped(DATABASE), pytest.raises(urllib.error.HTTPError) as raised:
        in_process.connect(DOWN, {}).request("no-such-ledger", "metadata")
    replay, error = failure(raised.value)
    assert (replay.code, error["code"]) == (404, "ledger_missing")
    assert server_calls == []


@pytest.mark.feature("tick-ledger-in-process")
def test_the_tick_ledger_port_reads_and_writes_in_process_with_the_ledger_server_down(
    slug: str, server_calls: list[object]
) -> None:
    port = ledger_adapter.ClientLedger(slug, ledger_adapter.resource_client(slug, service=True))
    deadline = Deadline(time.monotonic() + 30, time.monotonic)
    with in_process.scoped(DATABASE), api_client.kept_exports():
        port.mutate([_chat("the port wrote this")], deadline=deadline)
        exported = port.export(deadline=deadline)
    assert "the port wrote this" in json.dumps(exported)
    assert server_calls == []


@pytest.mark.feature("tick-ledger-in-process")
def test_the_service_tick_route_serves_its_pass_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SimpleNamespace(redis=SimpleNamespace(hget=lambda key, field: None), key=lambda slug, name: name)

    @ledger_route.service
    def run(store: object, slug: str) -> bool:
        return in_process.active()

    monkeypatch.setenv(URL, DATABASE[URL])
    assert run(store, "sw") is True
    monkeypatch.delenv(URL)
    assert run(store, "sw") is False
