import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import master_login_chain as chain


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
def test_browser_authorization_reports_through_master_inbox_and_ends_native_login(tmp_path, monkeypatch, outcome):
    from scripts.inbox import store
    from scripts.swarm import operator_email

    target = tmp_path / "slot" / "claude"
    target.mkdir(parents=True)
    env = {
        "AGENTIHOOKS_SWARM": "proof",
        "AGENTIHOOKS_AGENT_NAME": "engineer@proof",
        "AH_CC_TOKEN_worker": "test",
        "ANTHROPIC_API_KEY": "test",
    }
    events = []
    request = SimpleNamespace(id="request", state="done", reason="done: replied with message answer")
    answer = SimpleNamespace(id="answer", ref="request", sender="master@proof", text=outcome)

    class Inbox:
        def send(self, sender, recipient, text, **kwargs):
            events.append(("inbox", sender, recipient, text))
            return request

        def get(self, item):
            return {"request": request, "answer": answer}[item]

        def acts_for(self, sender, recipient):
            return sender == recipient

        def close(self, item, actor, kind, detail=""):
            events.append(("close", item, actor, kind))

    class Process:
        def __init__(self, argv, **kwargs):
            events.append(("process", argv, kwargs["env"]))
            assert kwargs["stdin"] == subprocess.DEVNULL
            kwargs["stdout"].write("https://claude.ai/oauth/authorize?state=proof\n")
            kwargs["stdout"].flush()
            self.returncode = 0 if outcome == "success" else None

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append(("terminate",))
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(store, "connect", lambda environ: Inbox())
    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr(operator_email, "send", lambda *_: pytest.fail("browser must not email"))
    assert chain.authorize(env, target, timeout=1) == outcome
    sent = next(event for event in events if event[0] == "inbox")
    assert sent[1:3] == ("engineer@proof", "master@proof")
    assert "already signed in" in sent[3] and "never sign in" in sent[3]
    assert "https://claude.ai/oauth/authorize?state=proof" in sent[3]
    argv, child = events[0][1:]
    assert argv[-2:] == ["auth", "login"]
    assert child["CLAUDE_CONFIG_DIR"] == child["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(target)
    assert "AH_CC_TOKEN_worker" not in child and "ANTHROPIC_API_KEY" not in child
    assert child["BROWSER"] == "true"
    assert (("terminate",) in events) == (outcome != "success")
    records = [json.loads(line) for line in (target.parent / "login-notifications.jsonl").read_text().splitlines()]
    assert records[-1] == {"step": "browser", "outcome": outcome, "transport": "inbox"}


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
    assert (target.parent / "browser-auth.log").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("cleanup", ["terminate", "kill", "already exited"])
def test_browser_timeout_cleans_up_native_auth_before_returning(tmp_path, monkeypatch, cleanup):
    from scripts.inbox import store

    target = tmp_path / "slot" / "claude"
    target.mkdir(parents=True)
    events = []

    class Process:
        def __init__(self, *args, **kwargs):
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
