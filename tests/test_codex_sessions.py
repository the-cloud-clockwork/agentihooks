import io
import json
import subprocess

import pytest

from scripts import codex_sessions

pytestmark = pytest.mark.unit

THREAD = "01a12c55-38bc-70c1-be9b-6c5d7fbcdc22"
OTHER = "01a12c48-90fa-7ac3-ba7f-d0eb33f15e38"


def _lookup(found):
    calls = []

    def lookup(name, archived, environ):
        calls.append((name, archived, environ))
        return found

    return lookup, calls


@pytest.mark.parametrize(
    ("args", "resolved", "archived"),
    [
        (["resume", "engineer@abc-0001"], ["resume", THREAD], False),
        (["archive", "engineer@abc-0001"], ["archive", THREAD], False),
        (["delete", "engineer@abc-0001"], ["delete", THREAD], False),
        (["fork", "engineer@abc-0001"], ["fork", THREAD], False),
        (["unarchive", "engineer@abc-0001"], ["unarchive", THREAD], True),
        (
            ["-c", "x=1", "-m", "gpt", "--no-daemon", "resume", "engineer@abc-0001", "go on"],
            ["-c", "x=1", "-m", "gpt", "--no-daemon", "resume", THREAD, "go on"],
            False,
        ),
    ],
)
def test_a_session_name_becomes_its_thread_id(args, resolved, archived):
    lookup, calls = _lookup([THREAD])
    assert codex_sessions.resolve(args, {"CODEX_HOME": "/h"}, lookup) == resolved
    assert calls == [("engineer@abc-0001", archived, {"CODEX_HOME": "/h"})]


@pytest.mark.parametrize(
    "args",
    [
        ["resume", THREAD],
        ["resume", THREAD.upper()],
        ["resume", "--last"],
        ["resume"],
        ["exec", "resume", "engineer@abc-0001"],
        ["write a poem about resume engineer"],
        ["-c", "resume", "engineer@abc-0001"],
        ["--config", "resume", "engineer@abc-0001"],
        ["-m", "resume", "engineer@abc-0001"],
        ["-p", "resume", "engineer@abc-0001"],
        ["-s", "resume", "engineer@abc-0001"],
        ["-a", "resume", "engineer@abc-0001"],
        ["-C", "resume", "engineer@abc-0001"],
        ["--model", "resume", "engineer@abc-0001"],
        ["--profile", "resume", "engineer@abc-0001"],
        ["--sandbox", "resume", "engineer@abc-0001"],
        ["--ask-for-approval", "resume", "engineer@abc-0001"],
        ["--cd", "resume", "engineer@abc-0001"],
        ["--enable", "resume", "engineer@abc-0001"],
        ["--disable", "resume", "engineer@abc-0001"],
        ["-c"],
        ["--no-daemon"],
        [],
    ],
)
def test_arguments_without_a_session_name_pass_unchanged(args):
    lookup, calls = _lookup([THREAD])
    assert codex_sessions.resolve(args, {}, lookup) == args
    assert calls == []


def test_a_flag_written_with_its_value_takes_no_next_argument():
    lookup, _ = _lookup([THREAD])
    assert codex_sessions.resolve(["-c=x=1", "resume", "n"], {}, lookup) == ["-c=x=1", "resume", THREAD]


@pytest.mark.parametrize("found", [[], [THREAD, OTHER]])
def test_a_name_with_no_single_match_is_left_for_codex_to_report(found):
    lookup, _ = _lookup(found)
    assert codex_sessions.resolve(["archive", "n"], {}, lookup) == ["archive", "n"]


def test_delete_finds_an_archived_session_when_no_active_one_matches():
    calls = []

    def lookup(name, archived, environ):
        calls.append(archived)
        return [THREAD] if archived else []

    assert codex_sessions.resolve(["delete", "n"], {}, lookup) == ["delete", THREAD]
    assert calls == [False, True]


def test_delete_leaves_a_name_found_nowhere_for_codex_to_report():
    calls = []
    assert codex_sessions.resolve(["delete", "n"], {}, lambda *args: calls.append(args[1]) or []) == ["delete", "n"]
    assert calls == [False, True]


class _Pipe(io.StringIO):
    flushed = ""

    def flush(self):
        self.flushed = self.getvalue()


class _Process:
    def __init__(self, replies):
        lines = (reply if isinstance(reply, str) else json.dumps(reply) for reply in replies)
        self.stdin = _Pipe()
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self.killed = self.waited = False

    def kill(self):
        self.killed = True

    def wait(self):
        self.waited = True
        return 0


def _thread(thread_id, name):
    return {"id": thread_id, "name": name, "preview": "x"}


def _server(replies, seen):
    def popen(command, **kwargs):
        process = _Process(replies)
        seen.update(command=command, kwargs=kwargs, process=process)
        return process

    return popen


def test_thread_ids_asks_the_codex_app_server_for_exact_name_matches(monkeypatch):
    monkeypatch.setattr(codex_sessions.shutil, "which", lambda name: f"/usr/bin/{name}")
    seen = {}
    replies = [
        "warning: not json",
        [1, 2],
        {"method": "configWarning", "params": {}},
        {"id": 0, "result": {"codexHome": "/h"}},
        {"id": 1, "result": {"data": [_thread(THREAD, "eng-a"), _thread(OTHER, "eng-ab"), {"id": "x"}]}},
    ]
    found = codex_sessions.thread_ids("eng-a", True, {"CODEX_HOME": "/h"}, _server(replies, seen))
    assert found == [THREAD]
    assert seen["command"] == ["/usr/bin/codex", "app-server"]
    assert seen["kwargs"] == {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "env": {"CODEX_HOME": "/h"},
    }
    sent = [json.loads(line) for line in seen["process"].stdin.getvalue().splitlines()]
    assert sent == [
        {"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "agentihooks", "version": "1"}}},
        {"method": "initialized"},
        {
            "id": 1,
            "method": "thread/list",
            "params": {
                "searchTerm": "eng-a",
                "archived": True,
                "modelProviders": [],
                "useStateDbOnly": True,
                "limit": 100,
            },
        },
    ]
    assert seen["process"].stdin.flushed == seen["process"].stdin.getvalue()
    assert seen["process"].killed and seen["process"].waited


def test_thread_ids_falls_back_to_codex_on_the_path(monkeypatch):
    monkeypatch.setattr(codex_sessions.shutil, "which", lambda name: None)
    seen = {}
    replies = [{"id": 1, "result": {"data": []}}]
    assert codex_sessions.thread_ids("n", False, {}, _server(replies, seen)) == []
    assert seen["command"] == ["codex", "app-server"]


@pytest.mark.parametrize(
    "replies",
    [
        [],
        ["not json"],
        [{"id": 1, "error": {"message": "no"}}],
        [{"id": 1, "result": {}}],
        [{"id": 1, "result": None}],
        [{"id": 1, "result": {"data": None}}],
    ],
)
def test_thread_ids_finds_nothing_when_the_server_gives_no_list(replies):
    seen = {}
    assert codex_sessions.thread_ids("n", False, {}, _server(replies, seen)) == []
    assert seen["process"].killed


def test_thread_ids_finds_nothing_when_codex_cannot_start():
    def popen(command, **kwargs):
        raise OSError("no codex")

    assert codex_sessions.thread_ids("n", False, {}, popen) == []


def test_thread_ids_stops_a_server_that_never_answers(monkeypatch):
    timers = []
    seen = {}

    class Timer:
        def __init__(self, seconds, action):
            timers.append(self)
            self.seconds, self.action, self.started, self.cancelled = seconds, action, False, False

        def start(self):
            self.started = seen["process"].killed is False
            self.action()

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(codex_sessions.threading, "Timer", Timer)
    assert codex_sessions.thread_ids("n", False, {}, _server([], seen)) == []
    assert [(t.seconds, t.started, t.cancelled) for t in timers] == [(20, True, True)]
    assert seen["process"].killed


def test_resolve_asks_the_codex_app_server_by_default(monkeypatch):
    asked = []
    monkeypatch.setattr(codex_sessions, "thread_ids", lambda *args: asked.append(args) or [THREAD])
    assert codex_sessions.resolve(["resume", "n"], {"CODEX_HOME": "/h"}) == ["resume", THREAD]
    assert asked == [("n", False, {"CODEX_HOME": "/h"})]
