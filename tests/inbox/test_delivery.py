import pytest

import hooks.context.inbox_delivery as delivery
from hooks import hook_manager
from hooks.targets.emitter import flush
from scripts.inbox.store import InboxError, InboxStore

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    store = InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    monkeypatch.setattr(delivery, "connect", lambda environ=None: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    return store


@pytest.fixture
def codex(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")


def _pre(capsys):
    hook_manager.on_pre_tool_use({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/"})
    flush("PreToolUse")
    return capsys.readouterr().out


def _post(capsys):
    hook_manager.on_post_tool_use(
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {"stdout": "a"},
            "cwd": "/",
        }
    )
    flush("PostToolUse")
    return capsys.readouterr().out


def test_claude_gets_a_pending_item_once_at_pre_tool_use_and_it_is_delivered(store, capsys):
    item = store.send("alice", "bob", "review my branch")
    out = _pre(capsys)
    assert "review my branch" in out and item.id in out and "alice" in out
    assert f"agentihooks msg close {item.id}" in out
    assert store.get(item.id).state == "delivered"
    assert [e["state"] for e in store.history(item.id)] == ["pending", "delivered"]
    assert "review my branch" not in _pre(capsys)


def test_codex_gets_the_item_at_post_tool_use_not_at_pre_tool_use(store, codex, capsys):
    item = store.send("alice", "bob", "review my branch")
    assert "review my branch" not in _pre(capsys)
    assert store.get(item.id).state == "pending"
    assert "review my branch" in _post(capsys)
    assert store.get(item.id).state == "delivered"
    assert "review my branch" not in _post(capsys)


def test_another_session_items_are_never_injected(store, capsys):
    item = store.send("alice", "carol", "for carol only")
    assert "for carol only" not in _pre(capsys)
    assert store.get(item.id).state == "pending"


def test_the_receiver_falls_back_to_the_session_id(store, monkeypatch, capsys):
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    item = store.send("alice", "s1", "to the session")
    assert "to the session" in _pre(capsys)
    assert store.get(item.id).state == "delivered"


def test_unreachable_redis_lets_the_tool_call_through_silently(store, monkeypatch, capsys):
    def unreachable(environ=None):
        raise InboxError("Redis is unreachable")

    store.send("alice", "bob", "review my branch")
    monkeypatch.setattr(delivery, "connect", unreachable)
    assert "review my branch" not in _pre(capsys)


def test_a_redis_failure_mid_call_lets_the_tool_call_through_silently(store, monkeypatch, capsys):
    import redis

    def broken(address):
        raise redis.ConnectionError("gone")

    store.send("alice", "bob", "review my branch")
    monkeypatch.setattr(store, "inbox", broken)
    assert "review my branch" not in _pre(capsys)
