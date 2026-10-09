import argparse
import io
import sys
import types
import urllib.error
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm_ledger import watch_ledger  # noqa: E402
from scripts.swarm_ledger.events import Expired, stream  # noqa: E402
from scripts.swarm_ledger.events.patch import diff  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "watchevents-2026-01-01"
MEMBERS = {"me": {"claims": ["phases/p1"]}, "boss": {"role": "orchestrator"}, "other": {}}
TASKS = [{"id": "t1", "claimed_by": "me"}, {"id": "t2", "claimed_by": "other"}]


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    import hooks._redis

    monkeypatch.setattr(hooks._redis, "get_redis", lambda: None)


def args(**changes):
    return argparse.Namespace(**{"slug": SLUG, "all": False, "name": None, "since_rev": 0, **changes})


def op(rev, target, by="operator", **extra):
    return {"rev": rev, "by": by, "kind": "comment added", "target": target, "id": f"c-{rev}", "text": "x", **extra}


def state(rev, events, **fields):
    return {"tasks": TASKS, **fields, "_meta": {"rev": rev, "events": events, "members": MEMBERS}}


def shown(capsys):
    return [line.split(" [")[1].split("]")[0] for line in capsys.readouterr().out.splitlines() if " [c-" in line]


class Marks:
    def __init__(self):
        self.calls = []

    def mark(self, name, ref):
        self.calls.append((name, ref))
        return True


def test_say_flushes_each_line(monkeypatch):
    class Out(io.StringIO):
        flushed = []

        def flush(self):
            self.flushed.append(self.getvalue())

    out = Out()
    monkeypatch.setattr(sys, "stdout", out)
    watch_ledger.say("one")
    assert out.flushed == ["one\n"]


def test_a_named_watcher_prints_only_the_operator_events_it_owes(capsys):
    events = [
        op(1, "tasks/t1"),
        op(2, "tasks/t2"),
        op(3, "phases/p1"),
        op(4, "phases/p9"),
        op(5, "tasks/t1", by="other"),
        {"by": "operator", "kind": "comment added", "target": "tasks/t1", "id": "c-0", "text": "x"},
    ]
    watch = watch_ledger.Watch(args(name="me"), "p.json", None)
    watch.show(state(5, events))
    assert shown(capsys) == ["c-1", "c-3"]


def test_an_unnamed_watcher_prints_every_operator_event_and_all_prints_agents_too(capsys):
    events = [op(1, "tasks/t2"), op(2, "phases/p9", by="other")]
    watch_ledger.Watch(args(), "p.json", None).show(state(2, events))
    assert shown(capsys) == ["c-1"]
    watch_ledger.Watch(args(all=True), "p.json", None).show(state(2, events))
    assert shown(capsys) == ["c-1", "c-2"]


def test_a_ledger_without_events_or_members_prints_only_the_watching_line(capsys):
    watch = watch_ledger.Watch(args(name="me"), "p.json", None)
    watch.show({"_meta": {"rev": 3}})
    assert capsys.readouterr().out == "WATCHING p.json rev 3\n"
    watch.show({"_meta": {"rev": 4, "events": [op(4, "tasks/t1")]}})
    assert shown(capsys) == ["c-4"]


def test_each_event_is_marked_once_for_the_watcher_and_ledger():
    marks = Marks()
    watch = watch_ledger.Watch(args(name="me"), "p.json", marks)
    watch.show(state(1, [op(1, "tasks/t1")]))
    assert marks.calls == [("me", f"{SLUG}:1:c-1")]


def test_chat_lines_carry_the_ledger_reply_rules_or_the_default(capsys):
    chat = op(1, "chat", kind="message added")
    watch_ledger.Watch(args(), "p.json", None).show(state(1, [chat], chat_instructions="Reply short."))
    assert capsys.readouterr().out.splitlines()[-1].endswith("| REPLY RULES: Reply short.")
    watch_ledger.Watch(args(), "p.json", None).show(state(1, [chat]))
    assert capsys.readouterr().out.splitlines()[-1].endswith(f"| REPLY RULES: {core.DEFAULT_CHAT_INSTRUCTIONS}")


def test_the_request_carries_the_credential_the_stream_accept_and_the_cursor(monkeypatch):
    monkeypatch.setattr(watch_ledger.ledger_link, "base", lambda: "http://h:1")
    given = {"X-Ledger-Token": "t"}
    assert watch_ledger.request(SLUG, None, given) == (
        f"http://h:1/api/v1/ledgers/{SLUG}/events",
        {"X-Ledger-Token": "t", "Accept": "text/event-stream"},
    )
    assert watch_ledger.request(SLUG, "c7", given)[1] == {
        "X-Ledger-Token": "t",
        "Accept": "text/event-stream",
        "Last-Event-ID": "c7",
    }
    assert given == {"X-Ledger-Token": "t"}
    assert watch_ledger.request("a/b c", None, {})[0] == "http://h:1/api/v1/ledgers/a%2Fb%20c/events"


def test_stream_opens_the_request_with_the_read_timeout_and_parses_frames(monkeypatch):
    opened = []
    body = stream.frame("snapshot", {"é": 1}, "c0") + stream.frame("heartbeat", {})

    def urlopen(request, timeout):
        opened.append((request.full_url, dict(request.header_items()), timeout))
        return io.BytesIO(body)

    monkeypatch.setattr(watch_ledger.ledger_link, "base", lambda: "http://h:1")
    monkeypatch.setattr(watch_ledger.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(watch_ledger, "credentials", lambda slug: {"X-Ledger-Token": f"for {slug}"})
    assert list(watch_ledger.stream(SLUG, "c1")) == [("snapshot", {"é": 1}, "c0"), ("heartbeat", {}, None)]
    assert list(watch_ledger.stream(SLUG, None, {"X-Ledger-Agent": "a"}))[0][2] == "c0"
    assert opened == [
        (
            f"http://h:1/api/v1/ledgers/{SLUG}/events",
            {"X-ledger-token": f"for {SLUG}", "Accept": "text/event-stream", "Last-event-id": "c1"},
            watch_ledger.STREAM_TIMEOUT_S,
        ),
        (f"http://h:1/api/v1/ledgers/{SLUG}/events", {"X-ledger-agent": "a", "Accept": "text/event-stream"}, 15.0),
    ]


@pytest.mark.parametrize(("code", "raised"), [(410, Expired), (403, urllib.error.HTTPError)])
def test_stream_turns_only_a_410_into_expired(monkeypatch, code, raised):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, code, "no", {}, None)

    monkeypatch.setattr(watch_ledger.urllib.request, "urlopen", urlopen)
    with pytest.raises(raised):
        next(watch_ledger.stream(SLUG, "c1", {}))


def test_credentials_come_from_the_ledger_cli_for_the_slug(monkeypatch):
    monkeypatch.setitem(sys.modules, "ledger", types.SimpleNamespace(credentials=lambda slug: {"slug": slug}))
    assert watch_ledger.credentials(SLUG) == {"slug": SLUG}


def test_alive_creates_the_beat_folder_and_touches_the_file(tmp_path):
    beat = tmp_path / "a" / "b" / "x.watch"
    watch_ledger.alive(beat)
    assert beat.exists()
    watch_ledger.alive(None)


@pytest.fixture
def ledger_file():
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
    html_path, json_path = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)
    return json_path


def run(monkeypatch, connections, *flags, sleeps_allowed=10):
    """main() over scripted connections; each is a list of frames or exceptions, the last one ends the run."""
    calls, credentials, sleeps = [], [], []

    def fake_stream(slug, cursor=None, headers=None):
        calls.append((slug, cursor, headers))
        for item in connections[len(calls) - 1]:
            if isinstance(item, BaseException):
                raise item
            yield item

    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) <= sleeps_allowed, "the watcher kept retrying"

    monkeypatch.setattr(watch_ledger, "stream", fake_stream)
    monkeypatch.setattr(watch_ledger, "credentials", lambda slug: credentials.append(slug) or {"n": len(credentials)})
    monkeypatch.setattr(watch_ledger, "time", types.SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(sys, "argv", ["watch_ledger.py", SLUG, "--interval", "0.5", *flags])
    with pytest.raises(SystemExit):
        watch_ledger.main()
    return calls, credentials, sleeps


def test_main_resumes_from_the_last_cursor_and_refreshes_credentials_only_after_a_failure(
    monkeypatch, capsys, ledger_file
):
    first = state(1, [])
    second = state(2, [op(2, "tasks/t1")])
    connections = [
        [
            ("snapshot", {"ledger": first}, "c0"),
            ("ledger", {"patch": diff(first, second)}, "c1"),
            ("heartbeat", {}, None),
            OSError("dropped"),
        ],
        [OSError("dropped")],
        [Expired()],
        [("snapshot", {"ledger": second}, "c5"), SystemExit()],
    ]
    calls, credentials, sleeps = run(monkeypatch, connections, "--since-rev", "0")
    assert calls == [(SLUG, None, {"n": 1}), (SLUG, "c1", {"n": 2}), (SLUG, "c1", {"n": 3}), (SLUG, None, {"n": 3})]
    assert credentials == [SLUG, SLUG, SLUG]
    assert sleeps == [0.5, 0.5]
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"WATCHING ledger {SLUG} rev 1",
        'OPERATOR rev=2 comment added on tasks/t1 [c-2]: "x"',
        "WARNING ledger stream: dropped",
    ]


def test_main_warns_again_after_a_recovery_and_resets_the_cursor_on_a_bad_patch(monkeypatch, capsys, ledger_file):
    first = state(1, [])
    connections = [
        [("ledger", {"patch": {"o": {}}}, "c1")],
        [OSError("down")],
        [("snapshot", {"ledger": first}, "c0"), OSError("down")],
        [SystemExit()],
    ]
    calls, _, sleeps = run(monkeypatch, connections)
    assert [cursor for _, cursor, _ in calls] == [None, None, None, "c0"]
    assert sleeps == [0.5, 0.5, 0.5]
    assert capsys.readouterr().out.splitlines().count("WARNING ledger stream: down") == 2


def test_a_named_main_marks_events_for_its_name_and_keeps_its_beat(monkeypatch, ledger_file):
    marks, slugs = Marks(), []
    monkeypatch.setattr(watch_ledger.seen, "marks_for", lambda slug: slugs.append(slug) or marks)
    monkeypatch.setattr(watch_ledger.signal, "signal", lambda *a: None)
    monkeypatch.setattr(watch_ledger.atexit, "register", lambda *a, **kw: None)
    beat = core.watch_path(SLUG, "me")
    beat.unlink(missing_ok=True)
    run(
        monkeypatch,
        [[("snapshot", {"ledger": state(1, [op(1, "tasks/t1")])}, "c0"), SystemExit()]],
        "--as",
        "me",
        "--since-rev",
        "0",
    )
    assert slugs == [SLUG]
    assert marks.calls == [("me", f"{SLUG}:1:c-1")]
    assert beat.exists()
    beat.unlink()
