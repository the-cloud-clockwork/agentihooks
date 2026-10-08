import json
import queue
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
from websockets.sync.server import unix_serve

import scripts.codex_app_server_probe as probe

PAUSE = {"method": "_pause"}
DEFAULT = "default"


class FakeClient:
    def __init__(self, name, replies, events):
        self.name = name
        self.replies = {method: list(answers) for method, answers in replies.items()}
        self.events = list(events)
        self.calls = []
        self.answers = []
        self.waits = []
        self.closed = False

    def request(self, method, params, timeout=DEFAULT):
        self.calls.append((method, params, timeout))
        reply = self.replies[method].pop(0)
        return reply(params) if callable(reply) else reply

    def until(self, pred, timeout=DEFAULT):
        self.waits.append(timeout)
        seen = []
        while self.events:
            msg = self.events.pop(0)
            if msg is PAUSE:
                break
            seen.append(msg)
            if pred(msg):
                return msg, seen
        return None, seen

    def answer(self, rid, result):
        self.answers.append((rid, result))

    def close(self):
        self.closed = True


@pytest.fixture
def fakes(monkeypatch):
    scripts = {}
    made = {}

    def factory(sock, name):
        assert sock == "SOCK"
        replies, events = scripts[name]
        made[name] = FakeClient(name, replies, events)
        return made[name]

    monkeypatch.setattr(probe, "Client", factory)
    return scripts, made


def ok(result):
    return {"result": result}


def err(message):
    return {"error": {"code": -32600, "message": message}}


def agent(text):
    return {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": text}}}


def item(phase, kind, status=None):
    body = {"type": kind, **({"status": status} if status else {})}
    return {"method": f"item/{phase}", "params": {"item": body}}


def done(turn, status="completed"):
    return {"method": "turn/completed", "params": {"turn": {"id": turn, "status": status}}}


def thread(tid="TH"):
    return ok({"thread": {"id": tid}})


def turn(tid, **extra):
    return ok({"turn": {"id": tid, **extra}})


def user_turn(tid, words):
    return {
        "id": tid,
        "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": words}, {"type": "image"}]},
            {"type": "agentMessage", "content": [{"type": "text", "text": "not a user text"}]},
        ],
    }


def history(*turns):
    return ok({"thread": {"turns": list(turns)}})


READ_ONLY = {"cwd": "REPO", "approvalPolicy": "never", "sandbox": "read-only"}


def start_call(repo, **extra):
    return ("thread/start", {**READ_ONLY, "cwd": repo, **extra}, DEFAULT)


def turn_call(tid, words, timeout=DEFAULT):
    return ("turn/start", {"threadId": tid, "input": [{"type": "text", "text": words}]}, timeout)


def test_the_turn_timeout_is_four_minutes():
    assert probe.TURN_TIMEOUT_S == 240


def test_helpers_build_fixture_input_and_pick_agent_text():
    assert probe.text("hi") == [{"type": "text", "text": "hi"}]
    assert probe.method_is("a")({"method": "a"}) is True
    assert probe.method_is("a")({"method": "b"}) is False
    seen = [
        agent("ONE"),
        {"method": "item/completed", "params": {"item": {"type": "agentMessage"}}},
        item("completed", "commandExecution"),
        {"method": "item/completed", "params": {}},
        {"method": "turn/started"},
    ]
    assert probe.agent_texts(seen) == ["ONE", ""]
    assert probe.is_approval_request({"id": 1, "method": "item/fileChange/requestApproval"}) is True
    assert probe.is_approval_request({"method": "item/fileChange/requestApproval"}) is False
    assert probe.is_approval_request({"id": 1, "method": "item/tool/call"}) is False
    assert probe.is_approval_request({"id": 1}) is False


def test_a_turn_that_cannot_start_reports_the_error(fakes):
    scripts, made = fakes
    scripts["n"] = ({"turn/start": [err("busy")]}, [])
    client = probe.Client("SOCK", "n")
    assert probe.run_turn(client, "TH", "go", effort="low") == {"start_error": {"code": -32600, "message": "busy"}}
    assert client.calls == [
        ("turn/start", {"threadId": "TH", "input": [{"type": "text", "text": "go"}], "effort": "low"}, DEFAULT)
    ]
    assert client.waits == []


def test_a_turn_that_never_completes_reports_no_status(fakes):
    scripts, _ = fakes
    scripts["n"] = ({"turn/start": [turn("T1")]}, [agent("A"), done("OTHER")])
    client = probe.Client("SOCK", "n")
    assert probe.run_turn(client, "TH", "go") == {
        "turn": "T1",
        "completed": False,
        "status": None,
        "agent_text": ["A"],
        "methods": ["item/completed", "turn/completed"],
    }


def test_conversation_records_two_turns_and_the_history(fakes, tmp_path):
    scripts, made = fakes
    repo = str(tmp_path)
    scripts["probe-conversation"] = (
        {
            "thread/start": [thread()],
            "turn/start": [turn("T1"), turn("T2")],
            "thread/read": [
                history(
                    user_turn("T1", "u1"),
                    {"id": "T2", "items": [{"type": "userMessage"}, *user_turn("T2", "u2")["items"]]},
                )
            ],
        },
        [agent("ALPHA"), done("T1"), item("started", "reasoning"), agent("BRAVO"), done("T2", "interrupted")],
    )
    record = probe.conversation("SOCK", repo)
    assert record == {
        "thread": "TH",
        "first_turn": {
            "turn": "T1",
            "completed": True,
            "status": "completed",
            "agent_text": ["ALPHA"],
            "methods": ["item/completed", "turn/completed"],
        },
        "idle_turn_start": {
            "turn": "T2",
            "completed": True,
            "status": "interrupted",
            "agent_text": ["BRAVO"],
            "methods": ["item/completed", "item/started", "turn/completed"],
        },
        "thread_read": {"turn_count": 2, "turn_ids_match": True, "user_texts": ["u1", "u2"]},
    }
    client = made["probe-conversation"]
    assert client.calls == [
        start_call(repo),
        turn_call("TH", "Fixture one. Reply with exactly the word ALPHA and nothing else."),
        turn_call("TH", "Fixture two. The thread is idle now. Reply with exactly the word BRAVO."),
        ("thread/read", {"threadId": "TH", "includeTurns": True}, DEFAULT),
    ]
    assert client.waits == [DEFAULT, DEFAULT]
    assert client.closed is True


def test_conversation_flags_history_whose_turn_ids_differ(fakes, tmp_path):
    scripts, _ = fakes
    scripts["probe-conversation"] = (
        {
            "thread/start": [thread()],
            "turn/start": [turn("T1"), turn("T2")],
            "thread/read": [history({"id": "T2"}, {"id": "T1"})],
        },
        [done("T1"), done("T2")],
    )
    assert probe.conversation("SOCK", str(tmp_path))["thread_read"] == {
        "turn_count": 2,
        "turn_ids_match": False,
        "user_texts": [],
    }


def test_conversation_reads_a_history_without_turns_as_empty(fakes, tmp_path):
    scripts, _ = fakes
    scripts["probe-conversation"] = (
        {"thread/start": [thread()], "turn/start": [turn("T1"), turn("T2")], "thread/read": [ok({"thread": {}})]},
        [done("T1"), done("T2")],
    )
    assert probe.conversation("SOCK", str(tmp_path))["thread_read"] == {
        "turn_count": 0,
        "turn_ids_match": False,
        "user_texts": [],
    }


def steer_script(rest):
    return (
        {
            "thread/start": [thread()],
            "turn/start": [turn("T"), turn("T", status="inProgress")],
            "turn/steer": [err("expected active turn"), ok({"turnId": "T"}), err("no active turn to steer")],
            "thread/read": [history({"id": "T"})],
        },
        [
            item("started", "reasoning"),
            item("started", "commandExecution"),
            item("completed", "commandExecution", "completed"),
            item("completed", "reasoning"),
            agent("CHARLIE DELTA ECHO"),
            done("OTHER"),
            done("T"),
            *rest,
        ],
    )


def test_steer_records_stale_matching_racing_and_late_steers(fakes, tmp_path):
    scripts, made = fakes
    repo = str(tmp_path)
    scripts["probe-steer"] = steer_script([agent("LATE"), done("T9")])
    record = probe.steer("SOCK", repo)
    assert record == {
        "thread": "TH",
        "busy_turn": "T",
        "command_started_before_steer": True,
        "steer_stale_expected_turn": {"code": -32600, "message": "expected active turn"},
        "steer_matching_expected_turn": {"turnId": "T"},
        "turn_start_while_busy": {"turn": {"id": "T", "status": "inProgress"}},
        "busy_turn_status": "completed",
        "command_item_status": ["completed"],
        "agent_text": ["CHARLIE DELTA ECHO"],
        "steer_after_completion": {"code": -32600, "message": "no active turn to steer"},
        "later_turn_completed": "completed",
        "later_agent_text": ["LATE"],
        "turns_in_history": 1,
    }
    client = made["probe-steer"]
    steering = "Fixture four, steering. Also include the word DELTA in your final reply."
    assert client.calls == [
        start_call(repo),
        turn_call("TH", probe.SLOW_PROMPT),
        ("turn/steer", {"threadId": "TH", "expectedTurnId": "stale-turn-id", "input": probe.text("x")}, DEFAULT),
        ("turn/steer", {"threadId": "TH", "expectedTurnId": "T", "input": probe.text(steering)}, DEFAULT),
        turn_call("TH", "Fixture five. Reply ECHO."),
        (
            "turn/steer",
            {"threadId": "TH", "expectedTurnId": "T", "input": probe.text("Fixture six after completion.")},
            DEFAULT,
        ),
        ("thread/read", {"threadId": "TH", "includeTurns": True}, DEFAULT),
    ]
    assert client.waits == [120, DEFAULT, 90]
    assert client.closed is True


def test_steer_without_a_command_or_a_later_turn(fakes, tmp_path):
    scripts, _ = fakes
    scripts["probe-steer"] = (
        {
            "thread/start": [thread()],
            "turn/start": [turn("T"), err("busy")],
            "turn/steer": [ok({"turnId": "S"}), err("stale"), ok({"turnId": "T"})],
            "thread/read": [ok({"thread": {}})],
        },
        [item("started", "reasoning"), item("completed", "commandExecution"), PAUSE, PAUSE],
    )
    record = probe.steer("SOCK", str(tmp_path))
    assert record["command_started_before_steer"] is False
    assert record["steer_stale_expected_turn"] == {"turnId": "S"}
    assert record["steer_matching_expected_turn"] == {"code": -32600, "message": "stale"}
    assert record["turn_start_while_busy"] == {"code": -32600, "message": "busy"}
    assert record["steer_after_completion"] == {"turnId": "T"}
    assert record["busy_turn_status"] is None
    assert record["later_turn_completed"] is None
    assert record["later_agent_text"] == []
    assert record["turns_in_history"] == 0


def test_the_slow_prompt_runs_one_sleep():
    assert probe.SLOW_PROMPT == (
        "Fixture three. Run the shell command `sleep 25` exactly once, wait for it to finish, "
        "then reply with the word CHARLIE. Follow any later instruction in this turn too."
    )


REQUEST = {"id": 7, "method": "item/commandExecution/requestApproval", "params": {}}
RESOLVED = {"method": "serverRequest/resolved", "params": {}}


def approval_scripts(a_events, b_events, a_resume_early=None):
    return {
        "probe-owner-a": (
            {"thread/start": [thread()], "turn/start": [turn("T")]},
            a_events,
        ),
        "probe-viewer-b": (
            {"thread/resume": [a_resume_early or err("no rollout found"), ok({})]},
            b_events,
        ),
    }


def test_the_first_answer_owns_an_approval_both_clients_see(fakes, tmp_path):
    scripts, made = fakes
    repo = str(tmp_path)
    (tmp_path / "approval-probe.txt").write_text("")
    scripts.update(
        approval_scripts(
            [{"method": "turn/started"}, {"id": 3, "method": "other"}, REQUEST, agent("FOXTROT"), done("T")],
            [{"id": 4, "method": "item/requestApprovalLater"}, REQUEST, RESOLVED],
        )
    )
    record = probe.approvals("SOCK", repo)
    assert record == {
        "thread": "TH",
        "viewer_resume_before_first_turn": {"code": -32600, "message": "no rollout found"},
        "viewer_resume": "ok",
        "request_method": "item/commandExecution/requestApproval",
        "delivered_to_a": True,
        "delivered_to_b": True,
        "same_request_id": True,
        "answered_by": "probe-owner-a",
        "resolved_seen_by_other": True,
        "turn_status": "completed",
        "file_created": True,
        "agent_text": ["FOXTROT"],
    }
    a, b = made["probe-owner-a"], made["probe-viewer-b"]
    assert a.calls == [
        start_call(repo, approvalPolicy="untrusted"),
        turn_call("TH", probe.APPROVAL_PROMPT),
    ]
    assert b.calls == [("thread/resume", {"threadId": "TH"}, DEFAULT)] * 2
    assert a.answers == [(7, {"decision": "decline"})]
    assert b.answers == [(7, {"decision": "accept"})]
    assert a.waits == [30, 120, 180]
    assert b.waits == [10, 20]
    assert (a.closed, b.closed) == (True, True)


def test_a_viewer_alone_owns_an_approval_it_alone_receives(fakes, tmp_path):
    scripts, made = fakes
    scripts.update(
        approval_scripts([PAUSE, PAUSE, RESOLVED, PAUSE, done("T")], [REQUEST, agent("X"), done("T")], ok({}))
    )
    record = probe.approvals("SOCK", str(tmp_path))
    assert record["viewer_resume_before_first_turn"] == "ok"
    assert record["delivered_to_a"] is False
    assert record["delivered_to_b"] is True
    assert record["same_request_id"] is False
    assert record["answered_by"] == "probe-viewer-b"
    assert record["resolved_seen_by_other"] is True
    assert record["turn_status"] == "completed"
    assert record["file_created"] is False
    assert record["agent_text"] == ["X"]
    assert made["probe-viewer-b"].answers == [(7, {"decision": "decline"})]
    assert made["probe-owner-a"].answers == []


def test_no_approval_request_keeps_the_record_short(fakes, tmp_path):
    scripts, made = fakes
    scripts.update(approval_scripts([PAUSE, PAUSE], [PAUSE]))
    scripts["probe-viewer-b"][0]["thread/resume"][1] = err("gone")
    record = probe.approvals("SOCK", str(tmp_path))
    assert record["viewer_resume"] == {"code": -32600, "message": "gone"}
    assert record["request_method"] is None
    assert "answered_by" not in record
    assert made["probe-owner-a"].answers == []


def test_the_approval_prompt_touches_one_file():
    assert probe.APPROVAL_PROMPT == (
        "Fixture seven. Run the shell command `touch approval-probe.txt` in the current folder, then reply FOXTROT."
    )


def test_detach_records_a_viewer_leaving_and_another_returning(fakes, tmp_path):
    scripts, made = fakes
    repo = str(tmp_path)
    scripts["probe-bridge"] = (
        {"thread/start": [thread()], "turn/start": [turn("G")]},
        [{"method": "turn/started"}, agent("GOLF"), done("G"), done("X", "failed"), done("H")],
    )
    scripts["probe-viewer"] = ({"thread/resume": [ok({})]}, [item("started", "reasoning")])
    scripts["probe-viewer-reopen"] = (
        {
            "thread/resume": [thread()],
            "thread/loaded/list": [ok({"data": ["TH"]})],
            "turn/start": [turn("H")],
            "thread/read": [history({"id": "G"}, {"id": "H"})],
        },
        [agent("HOTEL"), done("H")],
    )
    record = probe.detach("SOCK", repo)
    assert record == {
        "thread": "TH",
        "viewer_saw_live_item": True,
        "turn_after_viewer_closed": "completed",
        "agent_text": ["GOLF"],
        "reopen_resume_same_thread": True,
        "loaded_threads": {"data": ["TH"]},
        "reopened_viewer_turn": {
            "turn": "H",
            "completed": True,
            "status": "completed",
            "agent_text": ["HOTEL"],
            "methods": ["item/completed", "turn/completed"],
        },
        "bridge_saw_viewer_turn": True,
        "turns_in_history": 2,
    }
    bridge = made["probe-bridge"]
    assert bridge.calls == [start_call(repo), turn_call("TH", probe.SLOW_PROMPT.replace("CHARLIE", "GOLF"))]
    assert bridge.waits == [30, DEFAULT, 30]
    assert made["probe-viewer"].calls == [("thread/resume", {"threadId": "TH"}, DEFAULT)]
    assert made["probe-viewer"].waits == [60]
    reopen = made["probe-viewer-reopen"]
    assert reopen.calls == [
        ("thread/resume", {"threadId": "TH"}, DEFAULT),
        ("thread/loaded/list", {}, DEFAULT),
        turn_call("TH", "Fixture eight from the reopened viewer. Reply HOTEL."),
        ("thread/read", {"threadId": "TH", "includeTurns": True}, DEFAULT),
    ]
    assert all(made[name].closed for name in made)


def test_detach_with_nothing_seen(fakes, tmp_path):
    scripts, _ = fakes
    scripts["probe-bridge"] = ({"thread/start": [thread()], "turn/start": [turn("G")]}, [PAUSE, PAUSE, done("Q")])
    scripts["probe-viewer"] = ({"thread/resume": [ok({})]}, [])
    scripts["probe-viewer-reopen"] = (
        {
            "thread/resume": [err("gone")],
            "thread/loaded/list": [ok({"data": []})],
            "turn/start": [err("gone")],
            "thread/read": [ok({"thread": {}})],
        },
        [],
    )
    record = probe.detach("SOCK", str(tmp_path))
    assert record["viewer_saw_live_item"] is False
    assert record["turn_after_viewer_closed"] is None
    assert record["reopen_resume_same_thread"] is False
    assert record["bridge_saw_viewer_turn"] is False
    assert record["turns_in_history"] == 0


def test_compaction_records_whether_the_compacted_notice_arrives(fakes, tmp_path):
    scripts, made = fakes
    repo = str(tmp_path)
    scripts["probe-compact"] = (
        {"thread/start": [thread()], "turn/start": [turn("I"), turn("J")], "thread/compact/start": [ok({})]},
        [
            agent("INDIA"),
            done("I"),
            item("started", "contextCompaction"),
            {"method": "thread/status/changed"},
            PAUSE,
            agent("JULIET"),
            done("J"),
        ],
    )
    record = probe.compaction("SOCK", repo)
    assert record == {
        "thread": "TH",
        "first": {
            "turn": "I",
            "completed": True,
            "status": "completed",
            "agent_text": ["INDIA"],
            "methods": ["item/completed", "turn/completed"],
        },
        "compact_response": {},
        "compacted_notification": False,
        "methods": ["item/started", "thread/status/changed"],
        "after": {
            "turn": "J",
            "completed": True,
            "status": "completed",
            "agent_text": ["JULIET"],
            "methods": ["item/completed", "turn/completed"],
        },
    }
    client = made["probe-compact"]
    assert client.calls == [
        start_call(repo),
        turn_call("TH", "Fixture nine. Reply INDIA."),
        ("thread/compact/start", {"threadId": "TH"}, DEFAULT),
        turn_call("TH", "Fixture ten after compaction. Reply JULIET."),
    ]
    assert client.waits == [DEFAULT, 180, DEFAULT]


def test_compaction_sees_a_compacted_notice_and_an_error(fakes, tmp_path):
    scripts, _ = fakes
    scripts["probe-compact"] = (
        {"thread/start": [thread()], "turn/start": [turn("I"), turn("J")], "thread/compact/start": [err("no")]},
        [done("I"), {"method": "thread/compacted"}, done("J")],
    )
    record = probe.compaction("SOCK", str(tmp_path))
    assert record["compact_response"] == {"code": -32600, "message": "no"}
    assert record["compacted_notification"] is True
    assert record["methods"] == ["thread/compacted"]


def test_the_sandbox_command_probes_repo_ledger_redis_and_https():
    assert probe.SANDBOX_CMD == [
        "bash",
        "-c",
        "touch repo-write-probe 2>/dev/null && echo write=yes || echo write=no; "
        "curl -s -o /dev/null -m 5 -w 'ledger_http=%{http_code}\\n' http://127.0.0.1:8765/ || echo ledger_http=fail; "
        "(exec 3<>/dev/tcp/127.0.0.1/6379 && printf 'PING\\r\\n' >&3 && head -c 7 <&3) 2>/dev/null | tr -d '\\r\\n' "
        "| sed 's/^/redis=/' ; echo; "
        "curl -s -o /dev/null -m 5 -w 'external_https=%{http_code}\\n' https://pypi.org/simple/ || echo external_https=fail",
    ]


def test_sandbox_runs_every_policy_and_cleans_the_repo(fakes, tmp_path):
    scripts, made = fakes
    repo = tmp_path / "repo"
    repo.mkdir()

    def writes(params):
        (repo / "repo-write-probe").write_text("")
        return ok({"exitCode": 0, "stdout": " write=yes\nledger_http=200 \n"})

    replies = [ok({"exitCode": 0, "stdout": "write=no\n"}) for _ in range(5)] + [writes, err("denied")]
    scripts["probe-sandbox"] = ({"command/exec": replies}, [])
    record = probe.sandbox("SOCK", str(repo))
    assert list(record) == list(probe.SANDBOX_POLICIES)
    assert record["readOnly"] == {"error": None, "exit": 0, "stdout": ["write=no"], "repo_file_written": False}
    assert record["workspaceWrite(writableRoots=repo)"] == {
        "error": None,
        "exit": 0,
        "stdout": ["write=yes", "ledger_http=200"],
        "repo_file_written": True,
    }
    assert record["dangerFullAccess"] == {
        "error": {"code": -32600, "message": "denied"},
        "exit": None,
        "stdout": [],
        "repo_file_written": False,
    }
    assert not (repo / "repo-write-probe").exists()
    assert not (tmp_path / "outside").exists()
    policies = [params["sandboxPolicy"] for _, params, _ in made["probe-sandbox"].calls]
    assert policies == [
        {"type": "readOnly"},
        {"type": "readOnly", "networkAccess": True},
        {"type": "workspaceWrite"},
        {"type": "workspaceWrite", "networkAccess": True},
        {"type": "workspaceWrite", "networkAccess": True, "writableRoots": []},
        {"type": "workspaceWrite", "writableRoots": [str(repo)]},
        {"type": "dangerFullAccess"},
    ]
    assert {(m, p["cwd"], tuple(p["command"]), t) for m, p, t in made["probe-sandbox"].calls} == {
        ("command/exec", str(repo), tuple(probe.SANDBOX_CMD), 60)
    }
    assert made["probe-sandbox"].closed is True


def test_account_reads_the_login_without_values(fakes, tmp_path):
    scripts, made = fakes
    repo = str(tmp_path)
    scripts["probe-account"] = (
        {
            "account/read": [ok({"account": {"type": "chatgpt", "planType": "plus"}, "requiresOpenaiAuth": True})],
            "thread/start": [thread()],
            "turn/start": [turn("K")],
        },
        [agent("KILO"), done("K")],
    )
    record = probe.account("SOCK", repo)
    assert record == {
        "account_read_error": None,
        "account_type": "chatgpt",
        "plan_present": True,
        "requires_openai_auth": True,
        "turn": {
            "turn": "K",
            "completed": True,
            "status": "completed",
            "agent_text": ["KILO"],
            "methods": ["item/completed", "turn/completed"],
        },
    }
    assert made["probe-account"].calls[:2] == [("account/read", {}, DEFAULT), start_call(repo)]
    assert made["probe-account"].calls[2] == turn_call("TH", "Fixture eleven. Reply KILO.")


def test_account_without_a_login(fakes, tmp_path):
    scripts, _ = fakes
    scripts["probe-account"] = (
        {"account/read": [err("logged out")], "thread/start": [thread()], "turn/start": [err("auth")]},
        [],
    )
    record = probe.account("SOCK", str(tmp_path))
    assert record == {
        "account_read_error": {"code": -32600, "message": "logged out"},
        "account_type": None,
        "plan_present": False,
        "requires_openai_auth": None,
        "turn": {"start_error": {"code": -32600, "message": "auth"}},
    }


def test_bridge_and_seed_turns_for_the_terminal_attach(fakes, tmp_path):
    scripts, made = fakes
    repo = str(tmp_path)
    scripts["probe-bridge-tui"] = ({"thread/resume": [ok({})], "turn/start": [turn("L")]}, [done("L")])
    scripts["probe-tui-seed"] = ({"thread/start": [thread()], "turn/start": [turn("M")]}, [agent("MIKE"), done("M")])
    assert probe.bridge_turn("SOCK", "TH") == {
        "turn": "L",
        "completed": True,
        "status": "completed",
        "agent_text": [],
        "methods": ["turn/completed"],
    }
    assert made["probe-bridge-tui"].calls == [
        ("thread/resume", {"threadId": "TH"}, DEFAULT),
        turn_call("TH", "Fixture twelve from the bridge while the terminal is attached. Reply LIMA."),
    ]
    assert probe.new_thread("SOCK", repo) == {
        "thread": "TH",
        "turn": {
            "turn": "M",
            "completed": True,
            "status": "completed",
            "agent_text": ["MIKE"],
            "methods": ["item/completed", "turn/completed"],
        },
    }
    assert made["probe-tui-seed"].calls == [
        start_call(repo),
        turn_call("TH", "Fixture thirteen seeds the terminal thread. Reply MIKE."),
    ]
    assert made["probe-bridge-tui"].closed and made["probe-tui-seed"].closed


def hooks(*states):
    return ok(
        {
            "data": [
                {"hooks": [{"key": "a:Stop", "eventName": "Stop", "trustStatus": states[0], "currentHash": "h1"}]},
                {"hooks": [{"key": "b", "eventName": "SessionStart", "trustStatus": states[1], "currentHash": "h2"}]},
            ]
        }
    )


def test_trust_hooks_writes_the_current_hash_of_each_untrusted_hook(fakes, tmp_path):
    scripts, made = fakes
    repo = str(tmp_path)
    scripts["probe-hook-trust"] = (
        {"hooks/list": [hooks("untrusted", "trusted"), hooks("trusted", "trusted")], "config/batchWrite": [ok({})]},
        [],
    )
    assert probe.trust_hooks("SOCK", repo) == {
        "before": {"Stop": "untrusted", "SessionStart": "trusted"},
        "write_error": None,
        "after": {"Stop": "trusted", "SessionStart": "trusted"},
    }
    assert made["probe-hook-trust"].calls == [
        ("hooks/list", {"cwds": [repo]}, DEFAULT),
        (
            "config/batchWrite",
            {
                "edits": [{"keyPath": 'hooks.state."a:Stop".trusted_hash', "value": "h1", "mergeStrategy": "replace"}],
                "reloadUserConfig": True,
            },
            DEFAULT,
        ),
        ("hooks/list", {"cwds": [repo]}, DEFAULT),
    ]
    assert made["probe-hook-trust"].closed is True


def test_trust_hooks_writes_nothing_when_every_hook_is_trusted(fakes, tmp_path):
    scripts, made = fakes
    scripts["probe-hook-trust"] = ({"hooks/list": [hooks("trusted", "trusted")] * 2}, [])
    record = probe.trust_hooks("SOCK", str(tmp_path))
    assert record["write_error"] is None
    assert [method for method, _, _ in made["probe-hook-trust"].calls] == ["hooks/list", "hooks/list"]


def test_trust_hooks_reports_a_refused_write(fakes, tmp_path):
    scripts, _ = fakes
    scripts["probe-hook-trust"] = (
        {"hooks/list": [hooks("untrusted", "trusted")] * 2, "config/batchWrite": [err("read only")]},
        [],
    )
    record = probe.trust_hooks("SOCK", str(tmp_path))
    assert record["write_error"] == {"code": -32600, "message": "read only"}
    assert record["after"] == {"Stop": "untrusted", "SessionStart": "trusted"}


def test_every_scenario_is_registered():
    assert probe.SCENARIOS == {
        "transport": probe.transport,
        "conversation": probe.conversation,
        "steer": probe.steer,
        "approvals": probe.approvals,
        "detach": probe.detach,
        "compaction": probe.compaction,
        "sandbox": probe.sandbox,
        "account": probe.account,
        "new_thread": probe.new_thread,
        "trust_hooks": probe.trust_hooks,
    }


def test_main_writes_the_scenario_record(fakes, tmp_path, capsys):
    scripts, made = fakes
    scripts["probe-hook-trust"] = ({"hooks/list": [ok({"data": []})] * 2}, [])
    out = tmp_path / "trust.json"
    assert probe.main(["probe", "SOCK", str(tmp_path), "trust_hooks", str(out)]) == 0
    record = {"before": {}, "write_error": None, "after": {}}
    assert out.read_text() == json.dumps(record, indent=2) + "\n"
    assert capsys.readouterr().out == json.dumps(record) + "\n"
    assert made["probe-hook-trust"].calls[0] == ("hooks/list", {"cwds": [str(tmp_path)]}, DEFAULT)


def test_main_runs_a_bridge_turn_on_the_named_thread(fakes, tmp_path, capsys):
    scripts, made = fakes
    scripts["probe-bridge-tui"] = ({"thread/resume": [ok({})], "turn/start": [turn("L")]}, [done("L")])
    out = tmp_path / "bridge.json"
    assert probe.main(["probe", "SOCK", str(tmp_path), "bridge_turn", str(out), "TH9"]) == 0
    assert json.loads(out.read_text())["turn"] == "L"
    assert made["probe-bridge-tui"].calls[0] == ("thread/resume", {"threadId": "TH9"}, DEFAULT)


def test_main_prints_at_most_three_thousand_characters(fakes, tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(probe.SCENARIOS, "big", lambda sock, repo: {"k": "x" * 5000, "p": Path("/a")})
    out = tmp_path / "big.json"
    probe.main(["probe", "SOCK", str(tmp_path), "big", str(out)])
    assert json.loads(out.read_text())["p"] == "/a"
    assert capsys.readouterr().out == json.dumps({"k": "x" * 5000, "p": "/a"})[:3000] + "\n"


def serve(handler):
    folder = tempfile.TemporaryDirectory(prefix="cx")
    path = f"{folder.name}/s"
    server = unix_serve(handler, path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return folder, path, server


def wait_for(pred, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_client_speaks_json_rpc_over_a_unix_websocket(monkeypatch):
    monkeypatch.setattr(probe, "RESPONSE_TIMEOUT_S", 1)
    received = []

    def handler(ws):
        for raw in ws:
            msg = json.loads(raw)
            received.append(msg)
            if msg.get("method") == "initialize":
                ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}}))
            elif msg.get("method") == "echo":
                ws.send(json.dumps({"jsonrpc": "2.0", "method": "note", "params": {"n": 1}}))
                ws.send(json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ask", "params": {}}))
                ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": msg["params"]}))

    folder, path, server = serve(handler)
    try:
        client = probe.Client(path, "t")
        assert client.name == "t"
        assert client.request("echo", {"a": 1}) == {"jsonrpc": "2.0", "id": 2, "result": {"a": 1}}
        note, seen = client.until(probe.method_is("note"), 5)
        assert note == {"jsonrpc": "2.0", "method": "note", "params": {"n": 1}}
        assert seen == [note]
        ask, _ = client.until(lambda m: "id" in m, 5)
        assert ask["id"] == 99
        client.answer(99, {"decision": "accept"})
        client.notify("ping", {"x": 1})
        client.notify("bare")
        with pytest.raises(TimeoutError, match="^t: no response to 5$"):
            client.wait_response(5, 0.1)
        assert wait_for(lambda: len(received) == 6)
        client.close()
    finally:
        server.shutdown()
        folder.cleanup()
    assert received == [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "echo", "params": {"a": 1}},
        {"jsonrpc": "2.0", "id": 99, "result": {"decision": "accept"}},
        {"jsonrpc": "2.0", "method": "ping", "params": {"x": 1}},
        {"jsonrpc": "2.0", "method": "bare"},
    ]


def test_client_reports_a_broken_stream_as_closed(monkeypatch):
    monkeypatch.setattr(probe, "RESPONSE_TIMEOUT_S", 1)

    def handler(ws):
        for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") == "initialize":
                ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}))
                ws.send("not json")

    folder, path, server = serve(handler)
    try:
        client = probe.Client(path, "t")
        closed, _ = client.until(probe.method_is("_closed"), 5)
        assert closed == {"method": "_closed", "error": "JSONDecodeError"}
        client.close()
    finally:
        server.shutdown()
        folder.cleanup()


def test_client_waits_a_minute_by_default(monkeypatch):
    client = object.__new__(probe.Client)
    seen = []
    monkeypatch.setattr(probe.Client, "send", lambda self, method, params: seen.append((method, params)) or 4)
    monkeypatch.setattr(probe.Client, "wait_response", lambda self, rid, timeout: (rid, timeout))
    assert client.request("m", {"p": 1}) == (4, None)
    assert seen == [("m", {"p": 1})]


class Clock:
    def __init__(self, *readings):
        self.readings = list(readings)
        self.sleeps = []

    def monotonic(self):
        return self.readings.pop(0)

    def sleep(self, seconds):
        self.sleeps.append(seconds)


class EmptyQueue:
    def __init__(self):
        self.waits = []

    def get(self, timeout):
        self.waits.append(timeout)
        raise queue.Empty


def test_wait_response_stops_at_the_deadline_without_sleeping(monkeypatch):
    clock = Clock(0, 60)
    monkeypatch.setattr(probe, "time", clock)
    client = object.__new__(probe.Client)
    client.name, client.responses, client.lock = "c", {}, threading.Lock()
    with pytest.raises(TimeoutError, match="^c: no response to 3$"):
        client.wait_response(3)
    assert clock.sleeps == []


def test_wait_response_polls_every_twenty_milliseconds(monkeypatch):
    clock = Clock(0, 59.9, 60)
    monkeypatch.setattr(probe, "time", clock)
    client = object.__new__(probe.Client)
    client.name, client.responses, client.lock = "c", {}, threading.Lock()
    with pytest.raises(TimeoutError):
        client.wait_response(3, 60)
    assert clock.sleeps == [0.02]


def test_until_polls_the_queue_every_half_second_until_the_deadline(monkeypatch):
    clock = Clock(0, 5, 9.99, 10)
    monkeypatch.setattr(probe, "time", clock)
    client = object.__new__(probe.Client)
    client.events = EmptyQueue()
    assert client.until(probe.method_is("x"), 10) == (None, [])
    assert client.events.waits == [0.5, 0.5]


def test_client_reads_in_a_daemon_thread_without_a_message_size_cap(monkeypatch):
    monkeypatch.setattr(probe, "RESPONSE_TIMEOUT_S", 1)
    big = "x" * (2 * 1024 * 1024)

    def handler(ws):
        for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") == "initialize":
                ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}))
                ws.send(json.dumps({"jsonrpc": "2.0", "method": "big", "params": {"blob": big}}))

    folder, path, server = serve(handler)
    try:
        client = probe.Client(path, "t")
        assert client.reader.daemon is True
        got, _ = client.until(lambda m: m.get("method") in ("big", "_closed"), 5)
        assert got["method"] == "big"
        assert len(got["params"]["blob"]) == len(big)
        client.close()
    finally:
        server.shutdown()
        folder.cleanup()


def test_transport_shows_a_websocket_upgrade_and_a_closed_raw_json_stream():
    folder, path, server = serve(lambda ws: None)
    try:
        link = f"{folder.name}/link"
        Path(link).symlink_to(path)
        assert probe.transport(link, "REPO") == {
            "socket_is_symlink": True,
            "raw_json_line_reply": "closed",
            "websocket_upgrade_status": "HTTP/1.1 101 Switching Protocols",
        }
        assert probe.transport(path, "REPO")["socket_is_symlink"] is False
    finally:
        server.shutdown()
        folder.cleanup()


def test_first_reply_reads_only_the_status_line():
    def handler(conn):
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 400 Bad\r\nX: y\r\n\r\n")

    folder = tempfile.TemporaryDirectory(prefix="cx")
    path = f"{folder.name}/s"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        listener.listen(1)
        threading.Thread(target=lambda: handler(listener.accept()[0]), daemon=True).start()
        assert probe.first_reply(path, b"GET / HTTP/1.1\r\n\r\n", 3) == "HTTP/1.1 400 Bad"
    folder.cleanup()


def test_first_reply_is_empty_when_the_server_stays_silent():
    folder = tempfile.TemporaryDirectory(prefix="cx")
    path = f"{folder.name}/s"

    def silent_then_close(listener):
        conn = listener.accept()[0]
        time.sleep(1)
        conn.close()

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        listener.listen(1)
        threading.Thread(target=silent_then_close, args=(listener,), daemon=True).start()
        assert probe.first_reply(path, probe.RAW_LINE, 0.2) == "no reply"
    folder.cleanup()


def test_transport_waits_three_seconds_for_each_reply(monkeypatch):
    calls = []
    monkeypatch.setattr(probe, "first_reply", lambda sock, payload, wait_s: calls.append((payload, wait_s)) or "R")
    assert probe.transport("/no/such/socket", "REPO") == {
        "socket_is_symlink": False,
        "raw_json_line_reply": "R",
        "websocket_upgrade_status": "R",
    }
    assert calls == [(probe.RAW_LINE, 3), (probe.UPGRADE, 3)]


def test_the_raw_line_is_one_initialize_request():
    assert json.loads(probe.RAW_LINE) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"clientInfo": {"name": "raw", "version": "0"}},
    }
    assert probe.RAW_LINE.endswith(b"\n")
    assert probe.UPGRADE.startswith(b"GET / HTTP/1.1\r\n")
    assert probe.UPGRADE.endswith(b"Sec-WebSocket-Version: 13\r\n\r\n")
