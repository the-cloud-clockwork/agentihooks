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


def test_claim_delivers_an_item_a_newer_writer_stored_with_a_field_this_code_lacks(inbox):
    item = inbox.send("operator", "bob", "a comment on your task")
    inbox.redis.hset(inbox.key("item", item.id), "added_later", "x")
    assert [channel.event(pushed) for pushed in channel.claim(inbox, "bob")] == [channel.event(inbox.get(item.id))]
    assert inbox.get(item.id).state == "delivered"


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


def test_the_reply_tool_holds_a_reply_to_the_operator_to_the_chat_word_rules(inbox):
    item = inbox.send("operator", "bob", "status?")
    assert channel.answer(inbox, "bob", item.id, "done in run 1791318020592").startswith("not sent: chat refused")
    assert inbox.get(item.id).state == "pending"
    assert channel.answer(inbox, "bob", item.id, "All done, the change is merged.").startswith("sent to operator")


def test_launch_args_name_the_inbox_server_as_a_development_channel():
    mcp, flag = channel.launch_args("/opt/agentihooks", "/venv/bin/python")
    assert flag == "--dangerously-load-development-channels=server:inbox"
    assert mcp.startswith("--mcp-config=")
    server = json.loads(mcp.removeprefix("--mcp-config="))["mcpServers"]["inbox"]
    code = "import sys; sys.path.insert(0, '/opt/agentihooks'); from scripts.inbox.channel import main; main()"
    assert server == {"command": "/venv/bin/python", "args": ["-I", "-c", code]}


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


def test_the_waker_skips_finished_agents_and_names_each_swarm(redis, inbox):
    store = RedisStore(redis)
    for slug in ("a", "b"):
        store.create(SwarmConfig(slug, "/repo", 1, 1, state="running"))
    store.put_agent("a", AgentRecord("a-eng-1", "eng", "t1", pane_id="p1", harness="codex"))
    store.put_agent("a", AgentRecord("a-eng-2", "eng", "t2", pane_id="p2", harness="codex", state="finished"))
    store.put_agent("b", AgentRecord("b-eng-1", "eng", "t1", pane_id="p3", harness="codex"))
    sent = {name: inbox.send("operator", name, "hi").id for name in ("a-eng-1", "a-eng-2", "b-eng-1")}
    herdr = FakeHerdr({"p1": "idle", "p2": "idle", "p3": "idle"})
    assert waker.wake_all(store, inbox, herdr, 1_000_000, W) == [
        f"a: woke a-eng-1 for message {sent['a-eng-1']}",
        f"b: woke b-eng-1 for message {sent['b-eng-1']}",
    ]
    assert [p for p, _ in herdr.prompts] == ["p1", "p3"]


def test_the_waker_passes_its_window_and_quiet_window_on(redis, inbox):
    from scripts.swarm import idle

    store = RedisStore(redis)
    store.create(SwarmConfig("sw", "/repo", 1, 1, state="running"))
    store.put_agent("sw", CODEX)
    item = inbox.send("operator", "sw-eng-1", "hi")
    t = item.created_at
    inbox.note(item.id, wake.WOKEN, wake.BY, "an earlier wake", t)
    idle.prompted(redis, "sw", "sw-eng-1", t + 10)
    herdr = FakeHerdr({"p1": "idle"})
    assert waker.wake_all(store, inbox, herdr, t + 100, 500, quiet=50) == []
    assert waker.wake_all(store, inbox, herdr, t + 100, 50, quiet=500) == []
    assert waker.wake_all(store, inbox, herdr, t + 100, 50, quiet=50) == [f"sw: woke sw-eng-1 for message {item.id}"]


class FakePubSub:
    def __init__(self, messages):
        self.messages, self.subscribed, self.timeouts = list(messages), [], []

    def subscribe(self, name):
        self.subscribed.append(name)

    def get_message(self, timeout):
        self.timeouts.append(timeout)
        if not self.messages:
            raise KeyboardInterrupt
        return self.messages.pop(0)


def test_the_waker_runs_a_wake_pass_on_each_notify_only(redis, inbox, monkeypatch, capsys):
    store = RedisStore(redis)
    pubsub = FakePubSub([None, {"data": "sw-eng-1"}, None])
    monkeypatch.setattr(redis, "pubsub", lambda: pubsub)
    passes = []

    def wake_all(*args, **kwargs):
        assert args[0] is store and args[1].redis is redis
        passes.append((args[2], args[3:], kwargs))
        return ["sw: woke sw-eng-1 for message m1"]

    monkeypatch.setattr(waker, "wake_all", wake_all)
    herdr = FakeHerdr({})
    env = {wake.WINDOW_ENV: "60", wake.QUIET_ENV: "7"}
    with pytest.raises(KeyboardInterrupt):
        waker.run(store, herdr, lambda: 42, env)
    assert pubsub.subscribed == [NOTIFY] and pubsub.timeouts == [waker.RECHECK_S] * 4
    assert passes == [(herdr, (42, 60_000, 7_000), {})]
    assert capsys.readouterr().out == "sw: woke sw-eng-1 for message m1\n"


def test_wake_now_moves_past_agents_it_cannot_or_need_not_wake(inbox):
    paneless = AgentRecord("sw-eng-3", "eng", "t3", harness="codex")
    empty = AgentRecord("sw-eng-4", "eng", "t4", pane_id="p4", harness="codex")
    busy = AgentRecord("sw-eng-5", "eng", "t5", pane_id="p5", harness="codex")
    for name in ("sw-eng-3", "sw-eng-2", "sw-eng-5", "sw-eng-1"):
        inbox.send("operator", name, "hi")
    herdr = FakeHerdr({"p1": "idle", "p2": "idle", "p4": "idle", "p5": "working"})
    wake.wake_now(inbox, "sw", [paneless, CLAUDE, empty, busy, CODEX], herdr, 1_000_000, W)
    assert herdr.prompts == [("p1", wake.WAKE_TEXT)]


def test_wake_now_notes_the_wake_as_the_swarm(inbox):
    from scripts.swarm import idle

    item = inbox.send("operator", "sw-eng-1", "hi")
    idle.prompted(inbox.redis, "sw", "sw-eng-1", 1)
    wake.wake_now(inbox, "sw", [CODEX], FakeHerdr({"p1": "idle"}), 1_000_000, W, quiet=100)
    note = inbox.history(item.id)[-1]
    assert (note["event"], note["by"], note["reason"], note["at"]) == (
        wake.WOKEN,
        wake.BY,
        "prompted sw-eng-1 to read its inbox",
        1_000_000,
    )


def test_claim_closes_an_already_shown_write_naming_why(inbox):
    SeenMarks(inbox.redis).mark("bob", "sw:3:c1")
    item = inbox.send("operator", "bob", "a comment", ref="sw:3:c1")
    channel.claim(inbox, "bob")
    assert inbox.get(item.id).reason == "done: already shown through the ledger"


def test_the_hook_shows_every_claimed_item_with_a_blank_line_between(inbox, monkeypatch):
    from hooks.context import inbox_delivery

    first, second = inbox.send("alice", "bob", "one"), inbox.send("alice", "bob", "two")
    monkeypatch.setattr(inbox_delivery, "connect", lambda env: inbox)
    shown = inbox_delivery.pending_context("s1", {"AGENTIHOOKS_AGENT_NAME": "bob"})
    assert shown == inbox_delivery._render(inbox.get(first.id)) + "\n\n" + inbox_delivery._render(inbox.get(second.id))
