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
    gate = calls[0][3]
    assert calls == [("demo", None, [{"id": "a"}, {"id": "b"}], gate)]
    assert (type(gate).__name__, gate.slug, gate.inner.inner.inner) == ("Fence", "demo", "budget demo")
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


def test_health_reports_the_served_ledger_folder_without_a_server_process(quiet, tmp_path, monkeypatch):
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    h = handler("/healthz?probe=1")
    h.do_GET()
    assert [(code, json.loads(body), ctype) for code, body, ctype in h.sent] == [
        (200, {"dir": str(tmp_path)}, "application/json")
    ]


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
    threads, steps = [], []

    class Thread:
        def __init__(self, **kwargs):
            threads.append(kwargs)

        def start(self):
            pass

    class Server:
        def __init__(self, address, handler_class, bind_and_activate=True):
            pass

        def server_bind(self):
            steps.append("bind")

        def server_activate(self):
            steps.append("activate")

        def serve_forever(self, poll_interval):
            steps.append(("serve", poll_interval))

        def drained(self, timeout):
            steps.append(("drained", timeout))
            return True

        def server_close(self):
            steps.append("close")

    waits = []

    class Lease:
        def __init__(self, directory):
            pass

        def acquire(self, claimant, wait):
            waits.append(wait)
            return self

        def release(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", Thread)
    monkeypatch.setattr(server, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(server, "HUB", SimpleNamespace(drain=lambda: steps.append("drain")))
    monkeypatch.setattr(server.server_lifetime, "watch", lambda *args: threading.Event())
    monkeypatch.setattr(server.ports.swarm().ledger_writer, "WriterLease", Lease)
    adopted = []
    monkeypatch.setattr(server.legacy, "adopt", adopted.append)
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    server.serve()
    assert waits == [0.0]
    watching = threads[1].pop("kwargs")["stopping"]
    assert watching.is_set()
    assert threads[0] == {"target": server.adopt_disk_files, "daemon": True}
    assert threads[1]["target"].__wrapped__ is server.watch_ledgers
    assert [{**t, "args": tuple(type(a) for a in t["args"])} for t in threads[1:]] == [
        {"target": threads[1]["target"], "args": (Server,), "daemon": True},
        {"target": server.answer_reloads, "args": (Server,), "daemon": True},
        {"target": server.refresh_streams, "args": (list,), "daemon": True},
    ]
    assert adopted == [server.stored]
    assert steps == [
        "bind",
        "activate",
        ("serve", server.POLL_S),
        "drain",
        ("drained", server.DRAIN_S),
        "close",
    ]


def test_a_successor_warms_reports_ready_then_waits_for_the_lease_and_takes_the_carry(monkeypatch, tmp_path):
    steps = []
    listener = object()

    class Lease:
        def __init__(self, directory):
            pass

        def acquire(self, claimant, wait):
            steps.append(("acquire", wait))
            return self

        def release(self):
            steps.append("release")

    def listening(held):
        steps.append(("listening", held is listener))
        return SimpleNamespace(
            serve_forever=lambda poll: steps.append("serve"),
            drained=lambda timeout: True,
            server_close=lambda: None,
        )

    monkeypatch.setattr(server.server_handoff, "inherited", lambda: listener)
    monkeypatch.setattr(server.server_handoff, "announce", lambda held: steps.append(("announce", held)))
    monkeypatch.setattr(server.stored, "generations", lambda: {"demo": 5})
    monkeypatch.setattr(server, "warm_modules", lambda: steps.append("warm modules"))
    monkeypatch.setattr(server, "warm", lambda: steps.append("warm"))
    monkeypatch.setattr(server, "warm_streams", lambda: steps.append("warm streams"))
    monkeypatch.setattr(server, "take_carry", lambda: steps.append("take carry") or ["demo"])
    monkeypatch.setattr(server, "write_carry", lambda since: steps.append(("write carry", since)))
    monkeypatch.setattr(server, "listening", listening)
    monkeypatch.setattr(server.ports.swarm().ledger_writer, "WriterLease", Lease)
    monkeypatch.setattr(server.legacy, "adopt", lambda stored: steps.append("adopt"))
    monkeypatch.setattr(server, "adopt_unrelayed", lambda: None)
    threads = []
    monkeypatch.setattr(
        server.threading, "Thread", lambda **kwargs: threads.append(kwargs) or SimpleNamespace(start=lambda: None)
    )
    monkeypatch.setattr(server.server_lifetime, "watch", lambda *args: threading.Event())
    monkeypatch.setattr(server.server_code, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "HUB", SimpleNamespace(drain=lambda: None))
    monkeypatch.setattr(server, "HANDOFF", {"since": {"demo": 6}})
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    server.serve()
    assert [t["args"] for t in threads if t["target"] is server.refresh_streams] == [(["demo"],)]
    assert steps == [
        "warm modules",
        "warm",
        "warm streams",
        ("announce", b'{"demo": 5}'),
        ("acquire", server.LEASE_WAIT_S),
        "take carry",
        "adopt",
        ("listening", True),
        "serve",
        ("write carry", {"demo": 6}),
        "release",
    ]
    assert server.HANDOFF == {}


def test_a_successor_without_a_carry_refreshes_every_view_it_warmed(monkeypatch, tmp_path):
    class Lease:
        def __init__(self, directory):
            pass

        def acquire(self, claimant, wait):
            return self

        def release(self):
            pass

    monkeypatch.setattr(server.server_handoff, "inherited", lambda: object())
    monkeypatch.setattr(server.server_handoff, "announce", lambda held: None)
    monkeypatch.setattr(server.stored, "generations", lambda: {})
    monkeypatch.setattr(server, "warm_modules", lambda: None)
    monkeypatch.setattr(server, "warm", lambda: None)
    monkeypatch.setattr(server, "warm_streams", lambda: ["demo", "other"])
    monkeypatch.setattr(server, "take_carry", lambda: None)
    monkeypatch.setattr(
        server,
        "listening",
        lambda held: SimpleNamespace(
            serve_forever=lambda poll: None, drained=lambda timeout: True, server_close=lambda: None
        ),
    )
    monkeypatch.setattr(server.ports.swarm().ledger_writer, "WriterLease", Lease)
    monkeypatch.setattr(server.legacy, "adopt", lambda stored: None)
    monkeypatch.setattr(server, "adopt_unrelayed", lambda: None)
    threads = []
    monkeypatch.setattr(
        server.threading, "Thread", lambda **kwargs: threads.append(kwargs) or SimpleNamespace(start=lambda: None)
    )
    monkeypatch.setattr(server.server_lifetime, "watch", lambda *args: threading.Event())
    monkeypatch.setattr(server.server_code, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "HUB", SimpleNamespace(drain=lambda: None))
    monkeypatch.setattr(server, "HANDOFF", {})
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    server.serve()
    assert [t["args"] for t in threads if t["target"] is server.refresh_streams] == [(["demo", "other"],)]


def test_a_carry_that_cannot_be_written_still_releases_the_lease(monkeypatch, tmp_path):
    released = []

    class Lease:
        def __init__(self, directory):
            pass

        def acquire(self, claimant, wait):
            return self

        def release(self):
            released.append(True)

    def broken(since):
        raise OSError("disk full")

    monkeypatch.setattr(server.server_handoff, "inherited", lambda: None)
    monkeypatch.setattr(server, "write_carry", broken)
    monkeypatch.setattr(
        server,
        "listening",
        lambda held: SimpleNamespace(
            serve_forever=lambda poll: None, drained=lambda timeout: True, server_close=lambda: None
        ),
    )
    monkeypatch.setattr(server.ports.swarm().ledger_writer, "WriterLease", Lease)
    monkeypatch.setattr(server.legacy, "adopt", lambda stored: None)
    monkeypatch.setattr(server, "adopt_unrelayed", lambda: None)
    monkeypatch.setattr(server.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(server.server_lifetime, "watch", lambda *args: threading.Event())
    monkeypatch.setattr(server.server_code, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "HUB", SimpleNamespace(drain=lambda: None))
    monkeypatch.setattr(server, "HANDOFF", {"since": {}})
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    with pytest.raises(OSError, match="disk full"):
        server.serve()
    assert released == [True]


def test_requests_still_running_at_the_drain_bound_keep_the_lease_until_the_process_ends(monkeypatch, tmp_path, capsys):
    steps = []

    class Lease:
        def __init__(self, directory):
            pass

        def acquire(self, claimant, wait):
            return self

        def release(self):
            steps.append("release")

    monkeypatch.setattr(server.server_handoff, "inherited", lambda: None)
    monkeypatch.setattr(server, "write_carry", lambda since: steps.append(("write carry", since)))
    monkeypatch.setattr(
        server,
        "listening",
        lambda held: SimpleNamespace(
            serve_forever=lambda poll: None,
            drained=lambda timeout: steps.append(("drained", timeout)) or False,
            server_close=lambda: steps.append("close"),
        ),
    )
    monkeypatch.setattr(server.ports.swarm().ledger_writer, "WriterLease", Lease)
    monkeypatch.setattr(server.legacy, "adopt", lambda stored: None)
    monkeypatch.setattr(server, "adopt_unrelayed", lambda: None)
    monkeypatch.setattr(server.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(server.server_lifetime, "watch", lambda *args: threading.Event())
    monkeypatch.setattr(server.server_code, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "HUB", SimpleNamespace(drain=lambda: None))
    monkeypatch.setattr(server, "HANDOFF", {"since": {"demo": 6}})
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    server.serve()
    assert steps == [("drained", server.DRAIN_S), "close"]
    assert server.HANDOFF == {"since": {"demo": 6}}
    assert capsys.readouterr().err.endswith(
        "drain: requests still ran at the bound; the lease ends with this process\n"
    )


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
    monkeypatch.setattr("scripts.scheduler.process_timer.wait", sleep)
    with pytest.raises(StopIteration):
        server.watch_ledgers()
    assert slept == [2.0]


def test_a_bin_purge_that_fails_is_reported_and_the_watch_goes_on(monkeypatch, capsys):
    def tidy():
        raise OSError("read only folder")

    def sleep(seconds):
        raise StopIteration

    monkeypatch.setattr(server, "reloading", lambda: False)
    monkeypatch.setattr(server.ledger_bin, "tidy", tidy)
    monkeypatch.setattr(server, "bin_closed_without_swarm", lambda: None)
    monkeypatch.setattr(server, "sample_streams", lambda: None)
    monkeypatch.setattr(server, "scheduled_wait", sleep)
    with pytest.raises(StopIteration):
        server.watch_ledgers()
    assert capsys.readouterr().err.startswith("bin purge: read only folder\n")


def test_serve_follows_a_shared_database_on_its_own_daemon_thread(monkeypatch, tmp_path):
    threads = []

    class Thread:
        def __init__(self, **kwargs):
            threads.append(kwargs)

        def start(self):
            pass

    class Server:
        def __init__(self, address, handler_class, bind_and_activate=True):
            pass

        def server_bind(self):
            pass

        def server_activate(self):
            pass

        def serve_forever(self, poll_interval):
            pass

        def drained(self, timeout):
            return True

        def server_close(self):
            pass

    class Lease:
        def __init__(self, directory):
            pass

        def acquire(self, claimant, wait):
            return self

        def release(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", Thread)
    monkeypatch.setattr(server, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(server, "HUB", SimpleNamespace(drain=lambda: None))
    monkeypatch.setattr(server.server_lifetime, "watch", lambda *args: threading.Event())
    monkeypatch.setattr(server.ports.swarm().ledger_writer, "WriterLease", Lease)
    monkeypatch.setattr(server.legacy, "adopt", lambda repository: None)
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(server.storage, "shared", lambda: "postgresql://ledger@db.test/ledger")
    server.serve()
    assert [thread["daemon"] for thread in threads] == [True] * 5
    assert threads[0]["target"] is server.adopt_disk_files
    assert threads[1]["target"].__wrapped__ is server.watch_ledgers
    assert [t["target"] for t in threads[2:4]] == [server.answer_reloads, server.refresh_streams]
    assert threads[4]["target"].__wrapped__ is server.follow_shared


def test_the_shared_follow_reads_every_watched_ledger_each_quarter_second(monkeypatch, capsys):
    read, slept = [], []

    def current(slug):
        read.append(slug)
        if slug == "broken":
            raise ValueError("unreadable")

    def sleep(seconds):
        slept.append(seconds)
        raise StopIteration

    monkeypatch.setattr(server, "SCHEDULER_UNAVAILABLE", threading.Event())
    monkeypatch.setattr(server.HUB, "watched", lambda: ["broken", "kept"])
    monkeypatch.setattr(server.repository, "current", current)
    monkeypatch.setattr("scripts.scheduler.process_timer.wait", sleep)
    with pytest.raises(StopIteration):
        server.follow_shared()
    assert read == ["broken", "kept"]
    assert slept == [0.25]
    assert capsys.readouterr().err == "shared follow broken: unreadable\n"


class Conflict(Exception):
    def __init__(self, pid):
        self.holder = {"pid": pid}
        super().__init__(f"held by {pid}")


class Lease:
    def __init__(self, holders):
        self.holders, self.waits = list(holders), []

    def acquire(self, claimant, wait):
        self.waits.append((claimant, wait))
        if self.holders:
            raise Conflict(self.holders.pop(0))
        return self


WRITER = SimpleNamespace(owner=lambda: {"pid": 7}, WriterConflict=Conflict)


def test_a_successor_outwaits_its_predecessor_still_holding_the_lease(capsys):
    lease = Lease([41, 41])
    server.take_lease(lease, WRITER, listener=object(), predecessor=lambda: 41)
    assert lease.waits == [({"pid": 7}, server.LEASE_WAIT_S)] * 3
    assert capsys.readouterr().err == "reload: predecessor pid 41 still holds the writer lease; waiting\n" * 2


def test_a_successor_refuses_a_lease_another_process_holds():
    lease = Lease([99])
    with pytest.raises(SystemExit, match="held by 99"):
        server.take_lease(lease, WRITER, listener=object(), predecessor=lambda: 41)
    assert lease.waits == [({"pid": 7}, server.LEASE_WAIT_S)]


def test_a_fresh_server_refuses_a_held_lease_at_once():
    lease = Lease([41])
    with pytest.raises(SystemExit, match="held by 41"):
        server.take_lease(lease, WRITER, listener=None, predecessor=lambda: 41)
    assert lease.waits == [({"pid": 7}, 0.0)]


def test_a_free_lease_is_taken_on_the_first_try():
    lease = Lease([])
    server.take_lease(lease, WRITER, listener=None)
    assert lease.waits == [({"pid": 7}, 0.0)]
