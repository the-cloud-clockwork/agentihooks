import io
import json
import threading
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger import ledger_task_duplicates

HOST = "ledger.test"


def handler(path, **headers):
    h = server.Handler.__new__(server.Handler)
    h.path, h.headers, h.principal, h.sent = path, {"Host": HOST, **headers}, None, []
    h.send = lambda code, body, ctype: h.sent.append((code, body, ctype))
    return h


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {HOST})
    for name in ("relay_to_inbox", "doctor_phrase", "deliver_alerts"):
        monkeypatch.setattr(server, name, lambda *args: None)
    monkeypatch.setattr(server.talk, "Budget", lambda slug: f"budget {slug}")


def test_a_page_save_answers_applied_rejected_and_bounded_warnings(quiet, monkeypatch):
    calls = []
    warnings = ["w" * 1500, *(f"w{n}" for n in range(30))]

    def apply_ops(slug, changes, ops, gate):
        calls.append((slug, changes, ops, gate))
        return {"_meta": {"rev": 7, "warnings": warnings}}, ["b"]

    monkeypatch.setattr(server, "repository", SimpleNamespace(apply_ops=apply_ops))
    h = handler("/api/demo")
    h.reply_state("demo", ops=[{"id": "a"}, {"id": "b"}], refusals={"c": "refused c"})
    assert calls == [("demo", None, [{"id": "a"}, {"id": "b"}], "budget demo")]
    assert h.sent == [
        (
            200,
            json.dumps(
                {
                    "applied": ["a"],
                    "rejected": ["b", "c"],
                    "_meta": {"rev": 7, "warnings": ["w" * 1000, *(f"w{n}" for n in range(19))]},
                }
            ),
            "application/json",
        )
    ]


def test_the_retired_whole_ledger_read_answers_gone_with_the_new_address(quiet, monkeypatch):
    h = handler("/api/demo")
    h.exists = lambda slug: slug == "demo"
    h.do_GET()
    assert h.sent == [(410, "read the ledger from /api/v1/ledgers/demo", "text/plain")]


def test_a_ledger_request_is_judged_against_that_ledgers_token(quiet, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "repository", SimpleNamespace(token=lambda slug: f"token {slug}"))
    monkeypatch.setattr(server.authority, "principal", lambda *args: seen.append(args) or "operator")
    h = handler("/api/demo", **{"X-Ledger-Token": "given", "X-Ledger-Agent": "engineer@a1-1"})
    assert h.refused("demo") is False
    assert (seen, h.principal) == ([("token demo", "demo", "given", "engineer@a1-1")], "operator")


def test_a_save_to_a_ledger_without_tasks_checks_against_no_task_ids(quiet, monkeypatch):
    monkeypatch.setattr(server, "repository", SimpleNamespace(get_document=lambda slug: {"title": "Demo"}))
    body = json.dumps({"changes": [{"path": "title", "value": "Renamed"}]}).encode()
    h = handler("/api/demo", **{"Content-Type": "application/json", "Content-Length": str(len(body))})
    h.rfile, h.exists, h.refused = io.BytesIO(body), lambda slug: True, lambda slug=None: False
    h.reply_state = lambda *args: h.sent.append(args)
    h.do_PUT()
    assert h.sent == [("demo", [{"path": "title", "value": "Renamed"}], [], {}, ledger_task_duplicates.Screen())]


def test_serve_runs_the_ledger_watch_on_a_daemon_thread(monkeypatch, tmp_path):
    threads = []

    class Thread:
        def __init__(self, **kwargs):
            threads.append(kwargs)

        def start(self):
            pass

    class Server:
        def __init__(self, address, handler_class):
            pass

        def serve_forever(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", Thread)
    monkeypatch.setattr(server, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(server.server_lifetime, "watch", lambda *args: threading.Event())
    adopted = []
    monkeypatch.setattr(server.legacy, "adopt", adopted.append)
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    server.serve()
    assert threads == [{"target": server.watch_ledgers, "daemon": True}]
    assert adopted == [server.stored]


def test_the_ledger_watch_sleeps_two_seconds_between_passes(monkeypatch):
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        raise StopIteration

    monkeypatch.setattr(server, "reloading", lambda: False)
    monkeypatch.setattr(server, "code_stamp", lambda: 0)
    monkeypatch.setattr(server.ledger_bin, "tidy", lambda: None)
    monkeypatch.setattr(server, "bin_closed_without_swarm", lambda: None)
    monkeypatch.setattr(server, "sample_streams", lambda: None)
    monkeypatch.setattr(server.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        server.watch_ledgers()
    assert slept == [2.0]
