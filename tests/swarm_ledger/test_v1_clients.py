import io
import json
import sys
import urllib.error
import uuid

import pytest

from tests.swarm_ledger.test_api_v1 import authority_live, live, request  # noqa: F401
from tests.swarm_ledger.test_ledger_authority import SLUG, cli_ledger, server

pytestmark = pytest.mark.xdist_group("fakeredis")

import chat_ledger  # noqa: E402
import ledger_hook  # noqa: E402
import ledger_link  # noqa: E402

from scripts.swarm.ledger_client import LedgerClient  # noqa: E402
from scripts.swarm_ledger import chat_ledger as chat_ledger_module  # noqa: E402, F401
from scripts.swarm_ledger import ledger_hook as ledger_hook_module  # noqa: E402, F401


@pytest.fixture
def routes(live, monkeypatch):  # noqa: F811
    seen = []
    for method in ("do_GET", "do_POST", "do_PUT"):
        original = getattr(server.Handler, method)

        def record(self, original=original, method=method):
            seen.append((method.removeprefix("do_"), self.path.split("?", 1)[0]))
            return original(self)

        monkeypatch.setattr(server.Handler, method, record)
    monkeypatch.setattr(chat_ledger, "BASE", cli_ledger.BASE, raising=False)
    monkeypatch.setattr(ledger_link, "base", lambda: cli_ledger.BASE)
    return seen


def legacy(seen):
    return [entry for entry in seen if not entry[1].startswith("/api/v1/")]


def chat_rows():
    return request_rows("chat")


def chat_revision():
    from scripts.swarm_ledger.api.client import ResourceClient

    return ResourceClient(cli_ledger.BASE, cli_ledger.credentials(SLUG, service=True)).request(SLUG, "chat")["revision"]


def request_rows(path):
    from scripts.swarm_ledger.api.client import ResourceClient

    return ResourceClient(cli_ledger.BASE, cli_ledger.credentials(SLUG, service=True)).collection(SLUG, path)


def test_chat_command_posts_through_the_versioned_operations_route(routes, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["chat_ledger.py", SLUG, "--as", "api-reader", "Through v1"])
    chat_ledger.main()
    posted = json.loads(capsys.readouterr().out)["posted"]
    assert [row["text"] for row in chat_rows() if row["id"] == posted] == ["Through v1"]
    assert ("POST", f"/api/v1/ledgers/{SLUG}/operations") in routes
    assert legacy(routes) == []


def test_chat_command_reports_a_dead_server(monkeypatch):
    monkeypatch.setattr(cli_ledger, "BASE", "http://127.0.0.1:9")
    monkeypatch.setattr(chat_ledger, "BASE", "http://127.0.0.1:9", raising=False)
    monkeypatch.setattr(sys, "argv", ["chat_ledger.py", SLUG, "--as", "api-reader", "Nobody"])
    with pytest.raises(SystemExit) as exit_:
        chat_ledger.main()
    assert str(exit_.value).startswith("ledger server not answering on http://127.0.0.1:9: ")


def test_stop_gate_bypass_is_recorded_through_the_versioned_route(routes):
    ledger_hook.post_bypass({"slug": SLUG, "name": "api-reader"}, 3)
    events = [row for row in request_rows("events") if row.get("kind") == "gate bypassed"]
    assert [(row["by"], row["text"]) for row in events] == [("api-reader", "stopped with 3 unhandled operator events")]
    assert ("POST", f"/api/v1/ledgers/{SLUG}/operations") in routes
    assert legacy(routes) == []


def test_stop_gate_bypass_never_raises_when_the_server_is_gone(monkeypatch):
    logged = []
    monkeypatch.setattr(cli_ledger, "BASE", "http://127.0.0.1:9")
    monkeypatch.setattr(ledger_link, "base", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(ledger_hook, "log", logged.append)
    ledger_hook.post_bypass({"slug": SLUG, "name": "api-reader"}, 1)
    assert len(logged) == 1
    assert logged[0].startswith("bypass not recorded: ")


def test_tick_client_writes_and_reads_only_versioned_routes(routes):
    client = LedgerClient()
    client.say(SLUG, "From the tick", by="swarm")
    client.followup(SLUG, "Tick follow up")
    client.comment_phase(SLUG, "p1", "Tick phase note", by="swarm")
    assert "From the tick" in [row["text"] for row in client.chat(SLUG)]
    assert client.tasks(SLUG) == []
    assert client.closed(SLUG) is False
    assert any(path.endswith("/operations") for _, path in routes)
    assert legacy(routes) == []


def test_cli_retry_and_revision_conflict_keep_their_behaviour(routes):
    stale = request_rows("chat")
    revision = chat_revision()
    operation_id = uuid.uuid4().hex
    first = [{"op": "add", "id": "cli-retry", "thread": "chat", "text": "Once", "operation_id": operation_id}]
    applied = cli_ledger.call(SLUG, first)
    again = cli_ledger.call(SLUG, [dict(first[0])])
    assert applied["applied"] == again["applied"] == ["cli-retry"]
    assert [row["id"] for row in request_rows("chat")].count("cli-retry") == 1
    conflict = [{"op": "add", "id": "cli-stale", "thread": "chat", "text": "Stale", "expected_revision": revision}]
    with pytest.raises(SystemExit) as exit_:
        cli_ledger.call(SLUG, conflict)
    reply = str(exit_.value).removeprefix("server refused: 409 ")
    assert json.loads(reply)["error"]["code"] == "revision_conflict"
    assert "cli-stale" not in [row["id"] for row in request_rows("chat")]
    assert len(stale) + 1 == len(request_rows("chat"))
    assert legacy(routes) == []


class Recorder:
    def __init__(self, reply=None, error=None):
        self.calls, self.reply, self.error = [], reply, error

    def __call__(self, slug, ops, service=False):
        self.calls.append((slug, [dict(op) for op in ops], service))
        if self.error is not None:
            raise self.error
        return self.reply


def test_chat_command_sends_one_service_chat_operation(monkeypatch, capsys):
    recorder = Recorder({"applied": ["x"], "rejected": []})
    monkeypatch.setattr(cli_ledger, "request", recorder)
    monkeypatch.setattr(sys, "argv", ["chat_ledger.py", SLUG, "--as", "api-reader", "  Spaced  "])
    chat_ledger.main()
    [(slug, [op], service)] = recorder.calls
    assert (slug, service) == (SLUG, True)
    assert op["id"].startswith("m-") and len(op["id"]) == 12
    assert {key: value for key, value in op.items() if key != "id"} == {
        "op": "add",
        "thread": "chat",
        "text": "Spaced",
        "by": "api-reader",
    }
    assert capsys.readouterr().out == json.dumps({"posted": op["id"]}) + "\n"


def test_chat_command_exits_on_a_rejected_message(monkeypatch, capsys):
    monkeypatch.setattr(cli_ledger, "request", Recorder({"applied": [], "rejected": ["m-1"]}))
    monkeypatch.setattr(sys, "argv", ["chat_ledger.py", SLUG, "--as", "api-reader", "No"])
    with pytest.raises(SystemExit) as exit_:
        chat_ledger.main()
    assert str(exit_.value) == "message rejected: ['m-1']"
    assert capsys.readouterr().out == ""


def test_chat_command_exits_with_the_server_refusal(monkeypatch):
    body = io.BytesIO(b'{"error": {"code": "forbidden"}}')
    refusal = urllib.error.HTTPError(cli_ledger.BASE, 403, "Forbidden", {}, body)
    monkeypatch.setattr(cli_ledger, "request", Recorder(error=refusal))
    monkeypatch.setattr(sys, "argv", ["chat_ledger.py", SLUG, "--as", "api-reader", "No"])
    with pytest.raises(SystemExit) as exit_:
        chat_ledger.main()
    assert str(exit_.value) == 'server refused: 403 {"error": {"code": "forbidden"}}'


def test_stop_gate_bypass_sends_one_service_operation(monkeypatch):
    recorder = Recorder({"applied": [], "rejected": []})
    monkeypatch.setattr(cli_ledger, "request", recorder)
    ledger_hook.post_bypass({"slug": SLUG, "name": "api-reader"}, 4)
    [(slug, [op], service)] = recorder.calls
    assert (slug, service) == (SLUG, True)
    assert op["id"].startswith("gb-") and len(op["id"]) == 11
    assert {key: value for key, value in op.items() if key != "id"} == {
        "op": "gate_bypass",
        "by": "api-reader",
        "unhandled": 4,
    }
