import json

import pytest

from scripts.inbox import channel, wake, waker
from scripts.inbox.seen import SeenMarks
from scripts.inbox.store import NOTIFY, InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

W = 300_000


@pytest.fixture
def server():
    import fakeredis

    return fakeredis.FakeServer()


@pytest.fixture
def redis(server):
    import fakeredis

    return fakeredis.FakeRedis(server=server, decode_responses=True)


@pytest.fixture
def inbox(redis):
    return InboxStore(redis)


def notified(redis, act):
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(NOTIFY)
    pubsub.get_message(timeout=0.1)
    act()
    seen = []
    while (message := pubsub.get_message(timeout=0.1)) is not None:
        seen.append(message["data"])
    return seen


def test_a_send_publishes_its_address(redis, inbox):
    assert notified(redis, lambda: inbox.send("alice", "bob", "hi")) == ["bob"]


def test_a_redirect_publishes_the_new_address(redis, inbox):
    item = inbox.send("alice", "bob", "hi")
    assert notified(redis, lambda: inbox.redirect(item.id, "swarm", "carol", "moved")) == ["carol"]


def test_a_withdraw_publishes_nothing(redis, inbox):
    item = inbox.send("alice", "bob", "hi")
    assert notified(redis, lambda: inbox.withdraw(item.id, "swarm", "gone")) == []


def test_claim_delivers_each_pending_item_once(inbox):
    first = inbox.send("alice", "bob", "one")
    second = inbox.send("alice", "bob", "two")
    assert [i.id for i in channel.claim(inbox, "bob")] == [first.id, second.id]
    assert channel.claim(inbox, "bob") == []
    assert inbox.get(first.id).state == "delivered"


def test_claim_includes_the_seat_the_session_holds(inbox):
    inbox.seats.occupy("eng-1@sw", "eng@sw-1", 1)
    item = inbox.send("operator", "eng-1@sw", "for the seat")
    assert [i.id for i in channel.claim(inbox, "eng@sw-1")] == [item.id]


def test_claim_closes_a_ledger_write_already_shown(inbox):
    SeenMarks(inbox.redis).mark("bob", "sw:3:c1")
    item = inbox.send("operator", "bob", "a comment", ref="sw:3:c1")
    assert channel.claim(inbox, "bob") == []
    assert inbox.get(item.id).state == "done"


def test_an_item_becomes_a_channel_event_tagged_with_its_id_and_sender(inbox):
    item = inbox.send("alice", "bob", "review this")
    assert channel.event(item) == {
        "method": "notifications/claude/channel",
        "params": {
            "content": "review this",
            "meta": {"item_id": item.id, "sender": "alice", "sent_at_ms": str(item.created_at)},
        },
    }


def test_the_reply_tool_answers_the_sender_and_closes_the_item(inbox):
    item = inbox.send("alice", "bob", "ping")
    text = channel.answer(inbox, "bob", item.id, "pong")
    assert inbox.get(item.id).state == "done"
    [answer] = inbox.inbox("alice")
    assert answer.text == "pong" and answer.id in text


def test_the_reply_tool_reports_a_refusal_as_text(inbox):
    item = inbox.send("alice", "bob", "ping")
    assert "belongs to bob" in channel.answer(inbox, "mallory", item.id, "pong")
    assert inbox.get(item.id).state == "pending"


def test_launch_args_name_the_inbox_server_as_a_development_channel():
    mcp, flag = channel.launch_args("/opt/agentihooks", "/venv/bin/python")
    assert flag == "--dangerously-load-development-channels=server:inbox"
    assert mcp.startswith("--mcp-config=")
    server = json.loads(mcp.removeprefix("--mcp-config="))["mcpServers"]["inbox"]
    assert server["command"] == "/venv/bin/python"
    assert "/opt/agentihooks" in server["args"][-1] and "scripts.inbox.channel" in server["args"][-1]


class FakeHerdr:
    def __init__(self, status):
        self.status, self.prompts = dict(status), []

    def agent_status(self, agent):
        return self.status.get(agent.pane_id, "unknown")

    def typed_input(self, agent):
        return ""

    def prompt(self, agent, text):
        self.prompts.append((agent.pane_id, text))


CODEX = AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1", harness="codex", seat="eng-1@sw")
CLAUDE = AgentRecord("sw-eng-2", "eng", "t2", pane_id="p2", harness="claude")


def test_wake_now_prompts_an_idle_codex_pane_holding_an_item(inbox):
    item = inbox.send("operator", "sw-eng-1", "hello")
    herdr = FakeHerdr({"p1": "idle"})
    assert wake.wake_now(inbox, "sw", [CODEX], herdr, 1_000_000, W) == [f"woke sw-eng-1 for message {item.id}"]
    assert herdr.prompts == [("p1", wake.WAKE_TEXT)]
    assert [e.get("event") for e in inbox.history(item.id)] == [None, wake.WOKEN]


def test_wake_now_covers_the_seat_a_codex_agent_holds(inbox):
    inbox.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    inbox.send("operator", "eng-1@sw", "for the seat")
    herdr = FakeHerdr({"p1": "idle"})
    wake.wake_now(inbox, "sw", [CODEX], herdr, 1_000_000, W)
    assert herdr.prompts == [("p1", wake.WAKE_TEXT)]


def test_wake_now_leaves_busy_panes_claude_panes_and_woken_items(inbox):
    inbox.send("operator", "sw-eng-1", "hello")
    inbox.send("operator", "sw-eng-2", "hello")
    busy = FakeHerdr({"p1": "working", "p2": "idle"})
    assert wake.wake_now(inbox, "sw", [CODEX, CLAUDE], busy, 1_000_000, W) == []
    idle = FakeHerdr({"p1": "idle", "p2": "idle"})
    wake.wake_now(inbox, "sw", [CODEX, CLAUDE], idle, 1_000_000, W)
    wake.wake_now(inbox, "sw", [CODEX, CLAUDE], idle, 1_000_000, W)
    assert idle.prompts == [("p1", wake.WAKE_TEXT)]


def test_the_waker_wakes_codex_agents_of_running_swarms_only(redis, inbox):
    store = RedisStore(redis)
    store.create(SwarmConfig("sw", "/repo", 1, 1, state="running"))
    store.create(SwarmConfig("off", "/repo", 1, 1, state="paused"))
    store.put_agent("sw", CODEX)
    store.put_agent("off", AgentRecord("off-eng-1", "eng", "t1", pane_id="p9", harness="codex"))
    inbox.send("operator", "sw-eng-1", "hello")
    inbox.send("operator", "off-eng-1", "hello")
    herdr = FakeHerdr({"p1": "idle", "p9": "idle"})
    waker.wake_all(store, inbox, herdr, 1_000_000, W)
    assert herdr.prompts == [("p1", wake.WAKE_TEXT)]
