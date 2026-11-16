"""Every swarm ledger write runs the ledger API route code in process over the shared ledger store: the hub admission
claim and its release land in the store with no request to the ledger server, the ledger server answers every write
method as unsupported, and a hosted hub without the shared database refuses to start."""

import http.server
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import fakeredis
import pytest

from scripts.hub import admission, runner
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger, ledger_server, storage

SLUG = "write-cutover"
NAME = "eng@ab-1"
CONTENT = {"title": "Claims", "overview": "o", "phases": [{"title": "Claims", "description": "d"}]}
SEED = [
    {"op": "join", "id": "seed-join", "by": "boss", "role": "orchestrator"},
    {
        "op": "task_add",
        "id": "seed-task",
        "by": "boss",
        "task": "t1",
        "title": "Claim proof",
        "lane": "eng",
        "phase": "p1",
    },
]


class Recording(http.server.BaseHTTPRequestHandler):
    """A stand in ledger server that records every request and fails it."""

    seen: list[str] = []

    def answer(self) -> None:
        """Record the request and refuse it."""
        self.seen.append(f"{self.command} {self.path}")
        self.send_response(500)
        self.end_headers()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = answer

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the test output quiet."""


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """A recording server that every ledger server address of this process names."""
    seen: list[str] = []
    monkeypatch.setattr(Recording, "seen", seen)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recording)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{httpd.server_port}"
    monkeypatch.setenv("LEDGER_URL", address)
    monkeypatch.setattr(ledger, "BASE", address)
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    yield seen
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def store() -> Iterator[RedisStore]:
    """A swarm store holding the swarm and a ledger with one open task."""
    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(SLUG, "/repo", 2, 2))
    repository = storage.default()
    repository.create(SLUG, CONTENT)
    _, rejected = repository.apply_ops(SLUG, ops=SEED)
    assert rejected == []
    yield found
    found.redis.close()


def row(task_id: str) -> dict[str, Any]:
    """The task row as the shared ledger store holds it."""
    state, _ = storage.default().apply_ops(SLUG)
    return next(task for task in state["tasks"] if task["id"] == task_id)


@pytest.mark.feature("swarm-ledger-api-writes")
def test_the_hub_admission_claim_lands_in_the_ledger_store_with_no_ledger_server_request(
    recorder: list[str], store: RedisStore
) -> None:
    admitter = admission.Admission(store)
    live = admitter._mark(SLUG, {"task": "t1", "lane": "eng", "generation": 0}, {"claimed_by": ""}, NAME)
    assert live is not None
    assert (live["state"], live["claimed_by"]) == ("claimed", NAME)
    assert (row("t1")["state"], row("t1")["claimed_by"]) == ("claimed", NAME)
    admitter._release(SLUG, "t1", NAME)
    assert (row("t1")["state"], row("t1")["claimed_by"]) == ("open", "")
    assert recorder == []


@pytest.mark.feature("swarm-ledger-api-writes")
def test_a_stale_hub_claim_is_refused_by_the_route_with_no_ledger_server_request(
    recorder: list[str], store: RedisStore
) -> None:
    admitter = admission.Admission(store)
    assert admitter._mark(SLUG, {"task": "t1", "lane": "eng", "generation": 0}, {"claimed_by": ""}, NAME) is not None
    stale = admitter._mark(SLUG, {"task": "t1", "lane": "eng", "generation": 0}, {"claimed_by": ""}, "eng@ab-2")
    assert stale is None
    assert row("t1")["claimed_by"] == NAME
    assert recorder == []


@pytest.mark.feature("swarm-ledger-api-writes")
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_the_ledger_server_answers_every_write_method_as_unsupported(method: str) -> None:
    httpd = ledger_server.ThreadingHTTPServer(("127.0.0.1", 0), ledger_server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        sent = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/v1/ledgers/{SLUG}/operations",
            data=b'{"ops": []}',
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(sent, timeout=10)
        assert refused.value.code == 501
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.feature("swarm-ledger-api-writes")
def test_a_hosted_hub_without_the_shared_database_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(config: object) -> object:
        raise AssertionError("the hub opened its bus without the shared database")

    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    monkeypatch.setenv("LEDGER_URL", "https://ledger.example")
    monkeypatch.delenv("AGENTIHOOKS_SWARM_DATABASE_URL", raising=False)
    monkeypatch.setattr(runner, "open_bus", never)
    assert runner.main(["run", "--once"]) == 2
    assert "needs AGENTIHOOKS_SWARM_DATABASE_URL" in capsys.readouterr().err
