import json
import threading
from http.server import ThreadingHTTPServer

import ledger_core as core
import pytest

from scripts.inbox import outbox
from scripts.swarm_ledger import ledger, ledger_server, new_ledger

SLUG = "outbox-2026-01-01"
ENG = "eng-1@demo"
BOSS = "master@demo"


class Server:
    """The ledger server on a spare port; stop refuses every connection until start binds a new one."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.httpd = None

    def start(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), ledger_server.Handler)
        port = self.httpd.server_port
        self.monkeypatch.setattr(ledger_server, "ALLOWED_HOSTS", {f"127.0.0.1:{port}"})
        self.monkeypatch.setattr(ledger, "BASE", f"http://127.0.0.1:{port}")
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("LEDGER_AUTOSTART", "0")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    new_ledger.create(SLUG, {"title": "Outbox", "overview": "o", "phases": [{"title": "one"}], "questions": []})
    served = Server(monkeypatch)
    served.start()
    ledger.call(SLUG, [{"op": "join", "id": "join-boss", "by": BOSS, "role": "orchestrator"}])
    yield served
    served.stop()


def run(capsys, *argv):
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", argv[0], *argv[1:]])
    args.func(args)
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def queued():
    if not outbox.path().exists():
        return []
    return [json.loads(line) for line in outbox.path().read_text().splitlines()]


def stored():
    state, _ = core.sync(SLUG)
    return state


def comments(state):
    return [entry["text"] for entry in state["phases"][0]["comments"]]


def chat(state):
    return [entry["text"] for entry in state["chat"] if entry["by"] == ENG]


def test_talk_sent_while_the_server_is_stopped_queues_and_lands_once_in_order_when_it_returns(server, capsys):
    phase = f"phases/{stored()['phases'][0]['id']}"
    server.stop()
    assert run(capsys, ENG, "comment", phase, "The branch builds on the new base") == [
        {"posted": False, "queued": True}
    ]
    assert run(capsys, ENG, "say", "Tests run in continuous integration now") == [{"posted": False, "queued": True}]
    assert run(capsys, ENG, "question", "add", "Which port should the service use") == [{"question": "add"}]
    records = queued()
    assert [record["op"] for record in records] == ["ledger"] * 3
    assert [record["args"]["ops"][0]["op"] for record in records] == ["add", "add", "add_item"]
    assert [record["item"] for record in records] == [record["args"]["ops"][0]["id"] for record in records]
    assert comments(stored()) == []
    assert chat(stored()) == []
    assert stored()["questions"] == []

    server.start()
    ledger.call(SLUG)
    state = stored()
    assert comments(state) == ["The branch builds on the new base"]
    assert chat(state) == ["Tests run in continuous integration now"]
    assert [question["text"] for question in state["questions"]] == ["Which port should the service use"]
    assert queued() == []
    assert not outbox.path().exists()


def test_a_replayed_record_lands_once_even_when_it_is_replayed_again(server, capsys):
    phase = f"phases/{stored()['phases'][0]['id']}"
    server.stop()
    run(capsys, ENG, "comment", phase, "Only one copy of this lands")
    line = outbox.path().read_text()
    server.start()
    assert outbox.flush(None).applied == [json.loads(line)["item"]]
    outbox.path().write_text(line)
    assert outbox.flush(None).applied == [json.loads(line)["item"]]
    assert comments(stored()) == ["Only one copy of this lands"]
    assert not outbox.path().exists()


def test_an_answer_sent_while_the_server_is_stopped_lands_as_the_master_when_it_returns(server, capsys):
    run(capsys, ENG, "question", "add", "Which broker should we use")
    question = f"questions/{stored()['questions'][0]['id']}"
    server.stop()
    answer = {"op": "answer", "id": "answer-1", "by": BOSS, "item": question, "text": "Use the existing broker"}
    assert ledger.call(SLUG, [answer])["queued"] == "answer-1"
    assert queued()[0]["args"]["agent"] == ""
    server.start()
    ledger.call(SLUG)
    answers = stored()["questions"][0]["answers"]
    assert [(entry["by"], entry["text"]) for entry in answers] == [(BOSS, "Use the existing broker")]


def test_talk_sent_after_the_server_returns_lands_after_the_talk_queued_before_it(server, capsys):
    phase = f"phases/{stored()['phases'][0]['id']}"
    server.stop()
    run(capsys, ENG, "say", "First message")
    run(capsys, ENG, "say", "Second message")
    assert [record["args"]["ops"][0]["text"] for record in queued()] == ["First message", "Second message"]
    server.start()
    run(capsys, ENG, "comment", phase, "Third arrives after both")
    state = stored()
    assert chat(state) == ["First message", "Second message"]
    assert comments(state) == ["Third arrives after both"]
    assert queued() == []


def test_a_record_the_server_refuses_on_replay_is_set_aside_and_reported(server, capsys):
    server.stop()
    bad = {"op": "add", "thread": "phases/missing/comments", "id": "c-missing", "text": "Lost item", "by": ENG}
    assert ledger.call(SLUG, [bad])["queued"] == "c-missing"
    server.start()
    report = outbox.flush(None)
    assert report.rejected and report.rejected[0][0] == "c-missing"
    assert not outbox.path().exists()
    assert json.loads(outbox.skipped_path().read_text().splitlines()[-1])["line"].startswith('{"op": "ledger"')
    assert "refused on replay" in capsys.readouterr().err


def test_reads_and_untalkative_writes_still_fail_while_the_server_is_stopped(server):
    server.stop()
    with pytest.raises(SystemExit, match="ledger server not answering"):
        ledger.call(SLUG)
    with pytest.raises(SystemExit, match="ledger server not answering"):
        ledger.call(SLUG, [{"op": "leave", "id": "leave-1", "by": ENG}])
    assert queued() == []
