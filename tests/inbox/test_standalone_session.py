import json
import os

import pytest

from scripts.inbox import cli
from scripts.inbox.store import InboxStore

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SID = "standalone-session-id"
NAME = "solo-canary"


@pytest.fixture
def started(monkeypatch, tmp_path):
    import fakeredis

    import hooks.common as common
    from hooks import config, hook_manager

    store = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr("hooks.context.inbox_delivery.connect", lambda env: store)
    monkeypatch.setattr("hooks.context.broadcast._sessions_path", lambda: tmp_path / "active-sessions.json")
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", os.getpid)
    monkeypatch.setattr("hooks.lifecycle.deps_kick.kick", lambda: False)
    monkeypatch.setattr(common, "inject_context", lambda *a, **k: None)
    monkeypatch.setattr(config, "BROADCAST_ENABLED", True)
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", NAME)
    hook_manager.on_session_start({"hook_event_name": "SessionStart", "session_id": SID, "cwd": str(tmp_path)})
    return store


def test_a_started_init_agent_session_takes_mail_under_its_name(started, monkeypatch, capsys):
    from hooks.context.inbox_delivery import pending_context

    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "operator-shell")
    assert cli.main(["send", NAME, "canary", "ping"]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert (sent["to"], sent["state"]) == (NAME, "pending")
    context = pending_context(SID, environ={"AGENTIHOOKS_AGENT_NAME": NAME})
    assert f"=== INBOX: message {sent['id']} from operator-shell ===\ncanary ping" in context
    assert started.get(sent["id"]).state == "delivered"


def test_a_session_without_an_agent_name_stays_unnamed(started, monkeypatch, tmp_path):
    from hooks import hook_manager
    from hooks.context.broadcast import session_name

    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    hook_manager.on_session_start({"hook_event_name": "SessionStart", "session_id": "other", "cwd": str(tmp_path)})
    assert session_name(os.getpid()) == ""
