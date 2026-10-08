import argparse
import sys
import threading
import time
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.inbox import seen  # noqa: E402
from scripts.swarm_ledger import ledger_server as server  # noqa: E402
from scripts.swarm_ledger import watch_ledger  # noqa: E402
from scripts.swarm_ledger.events import Expired, Hub, stream  # noqa: E402
from scripts.swarm_ledger.repository import repository  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "watchstream-2026-01-01"


def make_ledger():
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
    html_path, json_path = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)


def note(n):
    return {"op": "add", "thread": "notes", "id": f"n-{n}", "text": f"note {n}"}


def args(**changes):
    return argparse.Namespace(**{"slug": SLUG, "all": False, "name": None, "since_rev": None, **changes})


def ledger(rev, *events):
    return {"tasks": [], "_meta": {"rev": rev, "events": list(events), "members": {}}}


def event(rev, n):
    return {"rev": rev, "by": "operator", "kind": "note added", "id": f"n-{n}", "text": f"note {n}"}


def test_the_first_snapshot_prints_the_watching_line_and_sets_the_start_revision(capsys):
    watch = watch_ledger.Watch(args(), "path.json", None)
    watch.take("snapshot", {"ledger": ledger(4, event(4, 1))})
    assert capsys.readouterr().out == "WATCHING path.json rev 4\n"
    assert watch.since == 4


def test_a_patch_prints_only_the_new_operator_events(capsys):
    watch = watch_ledger.Watch(args(since_rev=0), "path.json", None)
    watch.take("snapshot", {"ledger": ledger(1, event(1, 1))})
    from scripts.swarm_ledger.events.patch import diff

    watch.take("ledger", {"patch": diff(ledger(1, event(1, 1)), ledger(2, event(1, 1), event(2, 2))), "rev": 2})
    out = capsys.readouterr().out.splitlines()
    assert out[1:] == ['OPERATOR rev=1 note added [n-1]: "note 1"', 'OPERATOR rev=2 note added [n-2]: "note 2"']
    assert watch.since == 2


def test_a_reset_snapshot_never_prints_an_event_twice(capsys):
    watch = watch_ledger.Watch(args(since_rev=0), "path.json", None)
    watch.take("snapshot", {"ledger": ledger(1, event(1, 1))})
    watch.take("snapshot", {"ledger": ledger(3, event(1, 1), event(3, 3))})
    out = capsys.readouterr().out.splitlines()
    assert [line for line in out if line.startswith("OPERATOR")] == [
        'OPERATOR rev=1 note added [n-1]: "note 1"',
        'OPERATOR rev=3 note added [n-3]: "note 3"',
    ]
    assert sum(line.startswith("WATCHING") for line in out) == 1


def test_seed_errors_and_warnings_print_once_each(capsys):
    watch = watch_ledger.Watch(args(), "path.json", None)
    broken = ledger(1)
    broken["_meta"].update(seed_error="bad seed", warnings=["too long"])
    watch.take("snapshot", {"ledger": broken})
    watch.take("snapshot", {"ledger": broken})
    watch.take("snapshot", {"ledger": ledger(2)})
    assert capsys.readouterr().out.splitlines()[1:] == ["SEED_ERROR bad seed", "WARNING too long", "SEED_OK"]


def test_other_events_change_nothing(capsys):
    watch = watch_ledger.Watch(args(), "path.json", None)
    watch.take("heartbeat", {})
    watch.take("swarm", {"patch": {"v": {}}})
    assert watch.state is None and capsys.readouterr().out == ""


@pytest.fixture
def live(monkeypatch):
    make_ledger()
    monkeypatch.setattr(server, "HUB", Hub())
    monkeypatch.setattr(server, "swarm_status", lambda slug, state=None: None)
    monkeypatch.setattr(stream, "HEARTBEAT_S", 0.2)
    monkeypatch.setattr(seen, "marks_for", lambda slug, environ=None: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setattr(watch_ledger.ledger_link, "base", lambda: base)
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {f"127.0.0.1:{httpd.server_address[1]}"})
    yield base
    httpd.shutdown()
    httpd.server_close()


def test_stream_reads_the_snapshot_with_the_header_credential(live):
    frames = watch_ledger.stream(SLUG)
    name, data, cursor = next(frames)
    frames.close()
    assert name == "snapshot" and cursor
    assert data["ledger"]["_meta"]["rev"] == repository.get_document(SLUG)["_meta"]["rev"]


def test_stream_raises_expired_for_a_lost_cursor(live):
    with pytest.raises(Expired):
        next(watch_ledger.stream(SLUG, "bm9wZQ"))


def run_watch(monkeypatch, capsys, take, *flags):
    calls = []
    real_stream = watch_ledger.stream

    def recorded(slug, cursor=None, headers=None):
        calls.append(cursor)
        return real_stream(slug, cursor, headers)

    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) <= 20, "the watcher kept retrying"
        time.sleep(seconds)

    monkeypatch.setattr(watch_ledger, "stream", recorded)
    monkeypatch.setattr(watch_ledger, "time", types.SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(watch_ledger.Watch, "take", take)
    monkeypatch.setattr(sys, "argv", ["watch_ledger.py", SLUG, "--interval", "0.05", *flags])
    with pytest.raises(SystemExit):
        watch_ledger.main()
    return calls, capsys.readouterr().out


def test_the_watcher_prints_live_changes_and_replays_what_it_missed_after_a_drop(live, monkeypatch, capsys):
    start = repository.get_document(SLUG)["_meta"]["rev"]
    original = watch_ledger.Watch.take
    seen_names = []

    def take(self, name, data):
        original(self, name, data)
        seen_names.append(name)
        if seen_names == ["snapshot"]:
            server.repository.apply_ops(SLUG, ops=[note(1)])
        elif seen_names == ["snapshot", "ledger"]:
            server.repository.apply_ops(SLUG, ops=[note(2)])
            raise OSError("dropped")
        elif self.since >= start + 2:
            raise SystemExit

    calls, out = run_watch(monkeypatch, capsys, take, "--since-rev", str(start))
    assert calls[0] is None and calls[1] and len(calls) == 2
    assert seen_names.count("snapshot") == 1
    assert [line for line in out.splitlines() if line.startswith("OPERATOR")] == [
        f'OPERATOR rev={start + 1} note added [n-1]: "note 1"',
        f'OPERATOR rev={start + 2} note added [n-2]: "note 2"',
    ]


def test_an_expired_cursor_reconnects_without_one_and_prints_nothing_twice(live, monkeypatch, capsys):
    start = repository.get_document(SLUG)["_meta"]["rev"]
    server.repository.apply_ops(SLUG, ops=[note(3)])
    original = watch_ledger.Watch.take
    seen_names = []

    def take(self, name, data):
        original(self, name, data)
        seen_names.append(name)
        if seen_names == ["snapshot", "heartbeat"]:
            server.HUB.channels[SLUG].epoch = "restarted"
            raise OSError("server restarted")
        if seen_names.count("snapshot") == 2:
            raise SystemExit

    calls, out = run_watch(monkeypatch, capsys, take, "--since-rev", str(start))
    assert calls[0] is None and calls[1] and calls[2] is None
    assert seen_names == ["snapshot", "heartbeat", "snapshot"]
    assert out.count("[n-3]") == 1
    assert out.count("WATCHING") == 1


def test_the_beat_file_is_touched_by_every_heartbeat(live, monkeypatch, capsys):
    beat = core.watch_path(SLUG, "watcher")
    beat.unlink(missing_ok=True)
    stamps = []
    original = watch_ledger.alive

    def record(path):
        original(path)
        stamps.append(path.stat().st_mtime_ns)

    monkeypatch.setattr(watch_ledger, "alive", record)
    real_stream = watch_ledger.stream

    def short(slug, cursor=None, headers=None):
        for n, frame in enumerate(real_stream(slug, cursor, headers)):
            yield frame
            if n == 3:
                return

    monkeypatch.setattr(watch_ledger, "stream", short)
    monkeypatch.setattr(watch_ledger.time, "sleep", lambda seconds: (_ for _ in ()).throw(SystemExit))
    monkeypatch.setattr(sys, "argv", ["watch_ledger.py", SLUG, "--as", "watcher"])
    with pytest.raises(SystemExit):
        watch_ledger.main()
    assert len(stamps) == 5
    beat.unlink(missing_ok=True)


def test_a_missing_ledger_exits_before_connecting(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["watch_ledger.py", "absent-2026-01-01"])
    monkeypatch.setattr(watch_ledger, "stream", lambda *a: pytest.fail("connected"))
    with pytest.raises(SystemExit, match="no ledger absent-2026-01-01"):
        watch_ledger.main()


def test_the_watcher_connects_with_header_credentials_and_no_token_in_the_url(monkeypatch):
    seen = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            return iter([b"event: heartbeat\n", b"data: {}\n", b"\n"])

    def urlopen(request, timeout):
        seen.append((request.full_url, dict(request.header_items()), timeout))
        return Answer()

    monkeypatch.setattr(watch_ledger.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(watch_ledger.ledger_link, "base", lambda: "http://127.0.0.1:9")
    frames = list(watch_ledger.stream("a b", "cur", {"X-Ledger-Token": "secret"}))
    assert frames == [("heartbeat", {}, None)]
    url, headers, timeout = seen[0]
    assert url == "http://127.0.0.1:9/api/v1/ledgers/a%20b/events"
    assert headers == {"X-ledger-token": "secret", "Accept": "text/event-stream", "Last-event-id": "cur"}
    assert timeout == 3 * stream.HEARTBEAT_S


def test_a_failing_stream_warns_once_per_distinct_error(monkeypatch, capsys):
    make_ledger()
    errors = iter([OSError("refused"), OSError("refused"), ValueError("bad frame"), OSError("refused")])
    sleeps = []

    def failing(slug, cursor=None, headers=None):
        raise next(errors)
        yield

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 4:
            raise SystemExit

    monkeypatch.setattr(watch_ledger, "credentials", lambda slug: {})
    monkeypatch.setattr(watch_ledger, "stream", failing)
    monkeypatch.setattr(watch_ledger.time, "sleep", sleep)
    monkeypatch.setattr(sys, "argv", ["watch_ledger.py", SLUG, "--interval", "0.5"])
    with pytest.raises(SystemExit):
        watch_ledger.main()
    assert capsys.readouterr().out.splitlines() == [
        "WARNING ledger stream: refused",
        "WARNING ledger stream: bad frame",
        "WARNING ledger stream: refused",
    ]
    assert sleeps == [0.5] * 4


def test_the_credential_is_read_again_after_a_failure_and_reused_otherwise(monkeypatch, capsys):
    make_ledger()
    reads, used = [], []
    outcomes = iter(["ok", OSError("forbidden"), "ok", "stop"])

    def credentials(slug):
        reads.append(slug)
        if len(reads) == 1:
            raise OSError("page unreadable")
        return {"X-Ledger-Token": f"t{len(reads)}"}

    def flaky(slug, cursor=None, headers=None):
        used.append(headers["X-Ledger-Token"])
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "stop":
            raise SystemExit
        yield "heartbeat", {}, None

    monkeypatch.setattr(watch_ledger, "credentials", credentials)
    monkeypatch.setattr(watch_ledger, "stream", flaky)
    monkeypatch.setattr(watch_ledger.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sys, "argv", ["watch_ledger.py", SLUG])
    with pytest.raises(SystemExit):
        watch_ledger.main()
    assert used == ["t2", "t2", "t3", "t3"]
    assert len(reads) == 3


def test_a_patch_that_cannot_apply_reconnects_without_a_cursor(monkeypatch, capsys):
    make_ledger()
    calls = []

    def broken(slug, cursor=None, headers=None):
        calls.append(cursor)
        yield "ledger", {"patch": {"o": {}}, "rev": 9}, "c1"

    def sleep(seconds):
        if len(calls) == 2:
            raise SystemExit

    monkeypatch.setattr(watch_ledger, "credentials", lambda slug: {})
    monkeypatch.setattr(watch_ledger, "stream", broken)
    monkeypatch.setattr(watch_ledger.time, "sleep", sleep)
    monkeypatch.setattr(sys, "argv", ["watch_ledger.py", SLUG])
    with pytest.raises(SystemExit):
        watch_ledger.main()
    assert calls == [None, None]
