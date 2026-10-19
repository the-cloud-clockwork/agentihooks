import json

import pytest

from hooks.context import account_sessions, broadcast
from scripts.inbox.store import InboxStore
from scripts.swarm.resume import Outcome
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_close import closing  # noqa: F401


@pytest.fixture
def session(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    monkeypatch.setattr(broadcast, "BROADCAST_FILE", str(tmp_path / "broadcast.json"))
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 4242)
    return lambda name: broadcast.register_session("caller", 4242, "/repo", "sol", name=name)


def test_the_registered_master_closes_and_signs_without_an_exported_name(closing, session, capsys):  # noqa: F811
    store, ledger, _, _ = closing
    session("master@a1b2c3-0001")
    assert run("sw", "close", "--note", "Done here.") == 0
    assert ledger.calls == [("summary", "Done here.", "master@a1b2c3-0001"), ("closed", "master@a1b2c3-0001")]
    assert InboxStore(store.redis).inbox("master@a1b2c3-0001") == []
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["closed"] == "sw"


def test_reopen_is_signed_by_the_registered_session(env, session):  # noqa: F811
    _, ledger, _ = env
    reopened = []
    ledger.reopen = lambda slug, by: reopened.append((slug, by))
    run("sw", "create", "--repo", "/repo")
    session("master@a1b2c3-0001")
    assert run("sw", "reopen") == 0
    assert reopened == [("sw", "master@a1b2c3-0001")]


def test_send_message_comes_from_the_registered_session(env, session, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    session("engineer@a1b2c3-0001")
    capsys.readouterr()
    assert run("sw", "send-message", "pause new work") == 0
    [item] = InboxStore(store.redis).inbox("ci@a1b2c3-0001")
    assert item.sender == "engineer@a1b2c3-0001"


def test_a_registered_session_outside_the_swarm_may_not_choose_resume_or_fresh(env, session, monkeypatch):  # noqa: F811
    decided = []
    monkeypatch.setattr("scripts.swarm.resume.decide", lambda *a: decided.append(a) or Outcome("", "", "", "", ""))
    run("sw", "create", "--repo", "/repo")
    session("engineer@a1b2c3-0009")
    assert run("sw", "restore-decision", "engineer@a1b2c3-0001", "fresh") == 1
    assert decided == []
