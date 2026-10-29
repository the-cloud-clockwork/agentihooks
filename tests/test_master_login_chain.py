import io
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from scripts import master_login_chain as chain
from scripts import master_login_native as native


@pytest.mark.parametrize(
    ("available", "outcome", "expected", "order"),
    [
        (True, "failure", "session", ["current"]),
        (False, "success", "session", ["current", "browser", "current"]),
        (False, "captcha", "token", ["current", "browser", "token"]),
        (False, "failure", "token", ["current", "browser", "token"]),
    ],
)
def test_login_chain_uses_session_then_browser_then_token(available, outcome, expected, order):
    observed = []

    def current():
        observed.append("current")
        return "session" if available or observed.count("current") == 2 else None

    def browser():
        observed.append("browser")
        return outcome

    def token():
        observed.append("token")
        return "token"

    assert chain.run(current, browser, token) == expected
    assert observed == order


def test_browser_success_requires_a_live_session_before_use():
    observed = []

    def current():
        observed.append("current")
        return None

    def browser():
        observed.append("browser")
        return "success"

    def token():
        observed.append("token")
        return "token"

    assert chain.run(current, browser, token) == "token"
    assert observed == ["current", "browser", "current", "token"]


def test_pending_browser_never_falls_through_to_a_token():
    def token():
        pytest.fail("an unresolved browser request must wait")

    with pytest.raises(ValueError, match="^browser outcome must be success, captcha or failure$"):
        chain.run(lambda: None, lambda: "pending", token)


@pytest.mark.parametrize("outcome", ["success", "captcha", "failure"])
@pytest.mark.parametrize("origin", ["https://claude.ai/oauth/authorize", "https://claude.com/cai/oauth/authorize"])
@pytest.mark.parametrize("authorizer", ["", "browser-master@proof"])
def test_browser_authorization_reports_through_master_inbox_and_ends_native_login(
    tmp_path, monkeypatch, outcome, origin, authorizer
):
    from scripts.inbox import store
    from scripts.swarm import operator_email

    target = tmp_path / "slot" / "claude"
    target.mkdir(parents=True)
    env = {
        "AGENTIHOOKS_SWARM": "proof",
        "AGENTIHOOKS_AGENT_NAME": "engineer@proof",
        "AGENTIHOOKS_SWARM_TASK": "login-proof",
        "AH_CC_TOKEN_worker": "test",
        "ANTHROPIC_API_KEY": "test",
    }
    if authorizer:
        env["AGENTIHOOKS_MASTER_LOGIN_AUTHORIZER"] = authorizer
    recipient = authorizer or "master@proof"
    events = []
    request = SimpleNamespace(id="request", state="done", reason="done: replied with message answer")
    answer = SimpleNamespace(id="answer", ref="request", sender=recipient, text=outcome)

    class Inbox:
        def send(self, sender, recipient, text, **kwargs):
            events.append(("inbox", sender, recipient, text))
            assert kwargs == {"task": "login-proof", "item_id": "stable-request"}
            return request

        def get(self, item):
            return {"request": request, "answer": answer}[item]

        def acts_for(self, sender, recipient):
            return sender == recipient

        def close(self, item, actor, kind, detail=""):
            events.append(("close", item, actor, kind, detail))

    class Process:
        def __init__(self, argv, **kwargs):
            events.append(("process", argv, kwargs["env"]))
            assert kwargs["stdin"] == kwargs["stdout"] == subprocess.PIPE
            assert kwargs["stderr"] == subprocess.STDOUT
            assert kwargs["text"] is True
            self.stdin = io.StringIO()
            self.stdout = io.StringIO(f"{origin}?state=proof\n")
            self.returncode = None

        def poll(self):
            if outcome == "success" and self.stdin.getvalue() == "code#proof\n":
                self.returncode = 0
            return self.returncode

        def terminate(self):
            events.append(("terminate",))
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(
        store, "connect", lambda environ: Inbox() if environ is env else pytest.fail("wrong environment")
    )
    monkeypatch.setattr(store, "new_item_id", lambda: "stable-request")
    monkeypatch.setattr(
        native.shutil, "which", lambda name: {"claude": "/native/claude", "powershell.exe": "/powershell"}.get(name)
    )
    capture = Mock(return_value=SimpleNamespace(stdout="code#proof", returncode=0))
    monkeypatch.setattr(native.subprocess, "run", capture)
    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr(operator_email, "send", lambda *_: pytest.fail("browser must not email"))
    assert chain.authorize(env, target, timeout=1) == outcome
    sent = next(event for event in events if event[0] == "inbox")
    assert sent[1:3] == ("engineer@proof", recipient)
    assert sent[3] == (
        "Authorize this native master login using the already signed in claude.ai session in the operator browser connector; "
        "never sign in, sign out or switch accounts. Stop on a login form or CAPTCHA. "
        f"Open {origin}?state=proof and reply exactly success, captcha or failure. "
        "If shown a manual authorization code, click the page Copy button immediately before replying success; "
        "the matching native login will privately consume and clear the Windows clipboard. Do not snapshot the code. "
        "Never send authorization codes or credentials."
    )
    assert ("close", "answer", "engineer@proof", "done", "browser outcome consumed") in events
    argv, child = events[0][1:]
    from scripts import master_claude_login as login

    assert argv == ["/native/claude", *login.SETTINGS, "auth", "login"]
    assert child["CLAUDE_CONFIG_DIR"] == child["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(target)
    assert "AH_CC_TOKEN_worker" not in child and "ANTHROPIC_API_KEY" not in child
    assert child["BROWSER"] == "true"
    assert (("terminate",) in events) == (outcome != "success")
    assert capture.call_count == (1 if outcome == "success" else 0)
    records = [json.loads(line) for line in (target.parent / "login-notifications.jsonl").read_text().splitlines()]
    assert records == [
        {"step": "browser", "outcome": "requested", "transport": "inbox"},
        {"step": "browser", "outcome": outcome, "transport": "inbox"},
    ]
    assert (target.parent / "login-notifications.jsonl").stat().st_mode & 0o777 == 0o600
    assert not (target.parent / "browser-auth.log").exists()


def test_browser_request_without_a_master_seat_never_starts_auth(tmp_path, monkeypatch):
    target = tmp_path / "slot" / "claude"
    target.mkdir(parents=True)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("no master can authorize"))
    assert chain.authorize({}, target) == "failure"
    assert json.loads((target.parent / "login-notifications.jsonl").read_text()) == {
        "step": "browser",
        "outcome": "failure",
        "transport": "inbox",
    }


def test_native_auth_spawn_failure_is_recorded_without_an_email(tmp_path, monkeypatch):
    from scripts.inbox import store

    target = tmp_path / "slot" / "claude"
    target.mkdir(parents=True)
    monkeypatch.setattr(store, "connect", lambda _: object())

    def missing(*args, **kwargs):
        raise OSError("native auth unavailable")

    monkeypatch.setattr(subprocess, "Popen", missing)
    assert chain.authorize({"AGENTIHOOKS_SWARM": "proof"}, target) == "failure"
    assert json.loads((target.parent / "login-notifications.jsonl").read_text()) == {
        "step": "browser",
        "outcome": "failure",
        "transport": "inbox",
    }
    assert not (target.parent / "browser-auth.log").exists()


@pytest.mark.parametrize("cleanup", ["terminate", "kill", "already exited"])
def test_browser_timeout_cleans_up_native_auth_before_returning(tmp_path, monkeypatch, cleanup):
    from scripts.inbox import store

    target = tmp_path / "slot" / "claude"
    target.mkdir(parents=True)
    events = []

    class Process:
        def __init__(self, *args, **kwargs):
            self.stdin, self.stdout = io.StringIO(), io.StringIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append("terminate")
            if cleanup == "already exited":
                raise ProcessLookupError
            if cleanup == "terminate":
                self.returncode = -15

        def kill(self):
            events.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            if self.returncode is None:
                raise subprocess.TimeoutExpired("claude", timeout)
            return self.returncode

    monkeypatch.setattr(store, "connect", lambda _: object())
    monkeypatch.setattr(subprocess, "Popen", Process)
    env = {"AGENTIHOOKS_SWARM": "proof", "AGENTIHOOKS_AGENT_NAME": "engineer@proof"}
    assert chain.authorize(env, target, timeout=0) == "failure"
    expected = {
        "terminate": ["terminate", ("wait", 5)],
        "kill": ["terminate", ("wait", 5), "kill", ("wait", None)],
        "already exited": ["terminate"],
    }
    assert events == expected[cleanup]


@pytest.mark.parametrize("reply_ref,reply_sender", [("old request", "master@proof"), ("request", "other@proof")])
def test_browser_ignores_an_outcome_for_another_request_or_master(tmp_path, monkeypatch, reply_ref, reply_sender):
    from scripts.inbox import store

    target = tmp_path / "slot" / "claude"
    target.mkdir(parents=True)
    request = SimpleNamespace(id="request", state="done", reason="done: replied with message answer")
    answer = SimpleNamespace(id="answer", ref=reply_ref, sender=reply_sender, text="success")
    closed = []

    class Inbox:
        def send(self, *args, **kwargs):
            return request

        def get(self, item):
            return {"request": request, "answer": answer}[item]

        def acts_for(self, sender, recipient):
            return sender == recipient

        def close(self, *args):
            closed.append(args)

    def spawn(*args, **kwargs):
        return SimpleNamespace(
            stdin=io.StringIO(), stdout=io.StringIO("https://claude.ai/oauth/authorize?state=proof\n"), poll=lambda: 0
        )

    monkeypatch.setattr(store, "connect", lambda _: Inbox())
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(chain.time, "sleep", lambda _: None)
    env = {"AGENTIHOOKS_SWARM": "proof", "AGENTIHOOKS_AGENT_NAME": "engineer@proof"}
    assert chain.authorize(env, target, timeout=0.002) == "failure"
    assert closed == []


@pytest.mark.parametrize("failing_operation", ["send", "get"])
def test_inbox_outage_ends_native_auth_and_returns_browser_failure(tmp_path, monkeypatch, failing_operation):
    from redis.exceptions import RedisError

    from scripts.inbox import outbox, store

    target = tmp_path / "slot" / "claude"
    target.mkdir(parents=True)
    events = []

    class Inbox:
        def send(self, *args, **kwargs):
            if failing_operation == "send":
                raise RedisError("inbox disconnected")
            return SimpleNamespace(id="request")

        def get(self, item):
            raise RedisError("inbox disconnected")

        def close(self, *args):
            raise RedisError("inbox disconnected")

    class Process:
        def __init__(self, *args, **kwargs):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("https://claude.ai/oauth/authorize?state=proof\n")
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append("terminated")
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    queued = []
    monkeypatch.setattr(store, "new_item_id", lambda: "request")
    monkeypatch.setattr(store, "connect", lambda _: Inbox())
    monkeypatch.setattr(outbox, "waiting", lambda _: False)
    monkeypatch.setattr(outbox, "append", queued.append)
    monkeypatch.setattr(subprocess, "Popen", Process)
    env = {"AGENTIHOOKS_SWARM": "proof", "AGENTIHOOKS_AGENT_NAME": "engineer@proof"}
    assert chain.authorize(env, target, timeout=1) == "failure"
    assert events == ["terminated"]
    assert len(queued) == 1
    assert queued[0]["op"] == "close"
    assert queued[0]["by"] == "engineer@proof"
    assert queued[0]["item"] == "request"
    assert queued[0]["args"] == {"kind": "cancel", "detail": "browser attempt ended"}
    assert json.loads((target.parent / "login-notifications.jsonl").read_text().splitlines()[-1]) == {
        "step": "browser",
        "outcome": "failure",
        "transport": "inbox",
    }


def test_notification_appends_private_records_without_email(tmp_path):
    target = tmp_path / "claude"
    chain.notification(target, "current", "success")
    chain.notification(target, "token", "routed")
    path = tmp_path / "login-notifications.jsonl"
    assert path.stat().st_mode & 0o777 == 0o600
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"step": "current", "outcome": "success", "transport": "none"},
        {"step": "token", "outcome": "routed", "transport": "none"},
    ]


def test_token_fallback_emails_the_operator_once_per_login_attempt(tmp_path, monkeypatch):
    from scripts.swarm import operator_email

    target = tmp_path / "slot" / "claude"
    target.mkdir(parents=True)
    sent = []
    answers = ["email failed: down", "", ""]
    monkeypatch.setattr(operator_email, "send", lambda *args: sent.append(args) or answers.pop(0))
    first = {"AGENTIHOOKS_AGENT_NAME": "master@proof-1"}
    text = (
        "master@proof-1 runs on subscription token routing without Remote Control: "
        "the current claude.ai session failed and the browser authorization ended in captcha."
    )
    assert chain.alert(first, target, "captcha", "/repo") == "email failed: down"
    assert chain.alert(first, target, "captcha", "/repo") == ""
    assert chain.alert(first, target, "failure", "/repo") == ""
    expected = ("Master login fell back to token routing", text, first, "/repo")
    assert sent == [expected, expected]
    path = tmp_path / "slot" / "login-notifications.jsonl"
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {
            "step": "token",
            "outcome": "captcha",
            "transport": "refused",
            "agent": "master@proof-1",
            "reason": "email failed: down",
        },
        {"step": "token", "outcome": "captcha", "transport": "email", "agent": "master@proof-1"},
    ]
    second = {"AGENTIHOOKS_AGENT_NAME": "master@proof-2"}
    assert chain.alert(second, target, "failure", "/repo") == ""
    assert sent[-1] == (
        "Master login fell back to token routing",
        "master@proof-2 runs on subscription token routing without Remote Control: "
        "the current claude.ai session failed and the browser authorization ended in failure.",
        second,
        "/repo",
    )
    assert json.loads(path.read_text().splitlines()[-1]) == {
        "step": "token",
        "outcome": "failure",
        "transport": "email",
        "agent": "master@proof-2",
    }


def test_token_fallback_alert_ignores_other_records_of_the_same_agent(tmp_path, monkeypatch):
    from scripts.swarm import operator_email

    target = tmp_path / "claude"
    sent = []
    monkeypatch.setattr(operator_email, "send", lambda *args: sent.append(args) or "")
    chain.notification(target, "browser", "captcha", "inbox")
    with (tmp_path / "login-notifications.jsonl").open("a") as stream:
        stream.write(
            json.dumps({"step": "token", "outcome": "captcha", "transport": "none", "agent": "master@proof-1"}) + "\n"
        )
    assert chain.alert({"AGENTIHOOKS_AGENT_NAME": "master@proof-1"}, target, "captcha", "/repo") == ""
    assert len(sent) == 1


@pytest.mark.parametrize(
    "kind,detail",
    [
        ("cancel", "browser attempt ended"),
        ("done", "browser outcome consumed"),
    ],
)
@pytest.mark.parametrize("offline", [False, True])
def test_browser_close_keeps_the_actor_item_and_reason(monkeypatch, kind, detail, offline):
    from redis.exceptions import RedisError

    from scripts.inbox import outbox

    inbox = SimpleNamespace(close=Mock())
    if offline:
        inbox.close.side_effect = RedisError("offline")
    queued = []
    monkeypatch.setattr(outbox, "waiting", lambda _: False)
    monkeypatch.setattr(outbox, "append", queued.append)
    chain._close(inbox, "engineer@proof", "request", kind)
    inbox.close.assert_called_once_with("request", "engineer@proof", kind, detail)
    assert len(queued) == int(offline)
    if offline:
        assert queued[0]["by"] == "engineer@proof"
        assert queued[0]["item"] == "request"
        assert queued[0]["args"] == {"kind": kind, "detail": detail}


@pytest.mark.parametrize("state", ["pending", "done", "cancelled", "blocked"])
def test_finishing_browser_cancels_only_a_pending_request(monkeypatch, state):
    inbox = SimpleNamespace(get=Mock(return_value=SimpleNamespace(state=state)))
    close = Mock()
    monkeypatch.setattr(chain, "_close", close)
    chain._finish_request(inbox, "engineer@proof", "request")
    inbox.get.assert_called_once_with("request")
    assert close.call_args_list == ([call(inbox, "engineer@proof", "request", "cancel")] if state == "pending" else [])


@pytest.mark.parametrize(
    "state,reason,text,expected",
    [
        ("pending", "done: replied with message answer", "success", None),
        ("done", "unfinished", "success", None),
        ("done", "done: replied with message answer", "unexpected", "failure"),
    ],
)
def test_browser_reply_requires_a_settled_request_and_known_outcome(monkeypatch, state, reason, text, expected):
    request = SimpleNamespace(id="request", state=state, reason=reason)
    answer = SimpleNamespace(id="answer", ref="request", sender="master@proof", text=text)
    inbox = SimpleNamespace(
        get=Mock(side_effect=lambda item: {"request": request, "answer": answer}[item]),
        acts_for=lambda sender, recipient: sender == recipient,
    )
    close = Mock()
    monkeypatch.setattr(chain, "_close", close)
    assert chain._browser_reply(inbox, request, "master@proof", "engineer@proof") == expected
    assert close.call_args_list == ([call(inbox, "engineer@proof", "answer", "done")] if expected == "failure" else [])


@pytest.mark.parametrize(
    "scenario,codes,replies,expected,sleeps",
    [
        ("late link", [None, 0], ["success"], "success", 1),
        ("native wait", [None, 0], ["success"], "success", 1),
        ("reply wait", [0, 0], [None, "success"], "success", 1),
        ("native failure", [1], [None], "failure", 0),
        ("native error", [2], [None], "failure", 0),
        ("no link", [0], [], "failure", 0),
        ("browser failure", [], ["failure"], "failure", 0),
    ],
)
def test_browser_wait_orders_link_reply_and_native_completion(
    tmp_path, monkeypatch, scenario, codes, replies, expected, sleeps
):
    env = {"AGENTIHOOKS_SWARM": "proof", "AGENTIHOOKS_AGENT_NAME": "engineer@proof"}
    request = SimpleNamespace(id="request")
    inbox = object()
    process = SimpleNamespace(poll=Mock(side_effect=codes))
    requests = [None, request] if scenario == "late link" else [None] if scenario == "no link" else [request]
    make_request = Mock(side_effect=requests)
    reply = Mock(side_effect=replies)
    finish = Mock()
    sleep = Mock()
    ticks = iter(range(100))
    monkeypatch.setattr(chain.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(chain.time, "sleep", sleep)
    monkeypatch.setattr(chain, "_request", make_request)
    monkeypatch.setattr(chain, "_browser_reply", reply)
    monkeypatch.setattr(chain, "_finish_request", finish)
    target, output = tmp_path / "claude", tmp_path / "auth.log"
    assert chain._wait_for_browser(process, inbox, env, target, output, 10) == expected
    assert sleep.call_args_list == [call(0.25)] * sleeps
    assert make_request.call_args_list == [call(inbox, env, target, output)] * len(requests)
    assert reply.call_args_list == [call(inbox, request, "master@proof", "engineer@proof")] * len(replies)
    assert finish.call_args_list == ([] if scenario == "no link" else [call(inbox, "engineer@proof", "request")])
    assert process.poll.call_count == len(codes)


@pytest.mark.parametrize("reference,authorized", [("old", True), ("request", False)])
def test_browser_reply_rejects_each_identity_mismatch(reference, authorized):
    request = SimpleNamespace(id="request")
    current = SimpleNamespace(state="done", reason="done: replied with message answer")
    answer = SimpleNamespace(id="answer", ref=reference, sender="master@proof", text="success")
    close = Mock()
    store = SimpleNamespace(
        get=lambda item: current if item == "request" else answer, acts_for=lambda *_: authorized, close=close
    )
    assert chain._browser_reply(store, request, "master@proof", "engineer@proof") is None
    close.assert_not_called()


@pytest.mark.parametrize(
    "outcome,delivered,expected",
    [
        ("success", True, "success"),
        ("success", False, "failure"),
        ("captcha", True, "captcha"),
        ("failure", True, "failure"),
    ],
)
def test_browser_callback_is_consumed_only_after_matching_success(tmp_path, monkeypatch, outcome, delivered, expected):
    env = {"AGENTIHOOKS_SWARM": "proof", "AGENTIHOOKS_AGENT_NAME": "engineer@proof"}
    request = SimpleNamespace(id="request")
    events = []
    process = SimpleNamespace(poll=Mock(side_effect=[None, 0]))
    monkeypatch.setattr(chain, "_request", lambda *_: request)

    def reply(*_):
        events.append("matched reply")
        return outcome

    def complete():
        events.append("private callback")
        return delivered

    monkeypatch.setattr(chain, "_browser_reply", reply)
    monkeypatch.setattr(chain, "_finish_request", lambda *_: events.append("finished"))
    monkeypatch.setattr(chain.time, "sleep", lambda *_: None)
    assert chain._wait_for_browser(process, object(), env, tmp_path, object(), 1, complete) == expected
    assert events == (
        ["matched reply", "private callback", "finished"] if outcome == "success" else ["matched reply", "finished"]
    )


def test_browser_deadline_does_not_start_work_at_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(chain.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(chain, "_request", lambda *_: pytest.fail("deadline expired"))
    env = {"AGENTIHOOKS_SWARM": "proof", "AGENTIHOOKS_AGENT_NAME": "engineer@proof"}
    assert chain._wait_for_browser(None, None, env, tmp_path, tmp_path, 0) == "failure"


def test_native_authorization_uses_path_fallback_when_binary_is_missing(tmp_path, monkeypatch):
    from scripts import master_claude_login as login
    from scripts.inbox import store

    monkeypatch.setattr(native.shutil, "which", lambda _: None)
    monkeypatch.setattr(store, "connect", lambda _: object())
    spawned = Mock(return_value=SimpleNamespace(poll=lambda: 0, stdin=io.StringIO(), stdout=io.StringIO()))
    monkeypatch.setattr(chain.subprocess, "Popen", spawned)
    monkeypatch.setattr(chain, "_wait_for_browser", lambda *_: "failure")
    target = tmp_path / "claude"
    assert chain.authorize({"AGENTIHOOKS_SWARM": "proof"}, target) == "failure"
    assert spawned.call_args.args == (["claude", *login.SETTINGS, *login.LOGIN],)


def test_browser_request_without_a_task_keeps_a_stable_item_identity(tmp_path, monkeypatch):
    from scripts.inbox import store

    output = tmp_path / "auth.log"
    output.write_text("https://claude.ai/oauth/authorize?state=proof\n")
    target = tmp_path / "claude"
    env = {"AGENTIHOOKS_SWARM": "proof", "AGENTIHOOKS_AGENT_NAME": "engineer@proof"}
    inbox = SimpleNamespace(send=Mock(return_value="sent"))
    monkeypatch.setattr(store, "new_item_id", lambda: "request")
    assert chain._request(inbox, env, target, output) == "sent"
    assert inbox.send.call_args.kwargs == {"task": "", "item_id": "request"}


def test_a_transient_send_failure_cancels_the_request_immediately(tmp_path, monkeypatch):
    from redis.exceptions import RedisError

    from scripts.inbox import outbox, store

    output = tmp_path / "auth.log"
    output.write_text("https://claude.ai/oauth/authorize?state=proof\n")
    inbox = SimpleNamespace(send=Mock(side_effect=RedisError("temporary")), close=Mock())
    queued = Mock()
    monkeypatch.setattr(store, "new_item_id", lambda: "request")
    monkeypatch.setattr(outbox, "waiting", lambda _: False)
    monkeypatch.setattr(outbox, "append", queued)
    env = {"AGENTIHOOKS_SWARM": "proof", "AGENTIHOOKS_AGENT_NAME": "engineer@proof"}
    with pytest.raises(RedisError, match="temporary"):
        chain._request(inbox, env, tmp_path / "claude", output)
    inbox.close.assert_called_once_with("request", "engineer@proof", "cancel", "browser attempt ended")
    queued.assert_not_called()
