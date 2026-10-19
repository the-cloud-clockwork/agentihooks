from types import SimpleNamespace

import pytest

from scripts.inbox import channel, cli
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import delivery, ledger_client, operator_mail
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger.watch_ledger import line

SLUG = "sw"
ENG = AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1", seat=f"eng-1@{SLUG}")
BOSS = AgentRecord("sw-master-1", MASTER, MASTER, pane_id="pm", seat=f"master@{SLUG}")
DOC = {"tasks": [{"id": "t1", "state": "claimed", "claimed_by": ENG.name}]}
SIXTY_WORDS = " ".join(["merged"] * 60)


class PageLedger:
    def __init__(self):
        self.said, self.commented, self.ids = [], [], []

    def relay(self, slug, text, by, op_id=""):
        self.said.append((slug, text, by))

    def relay_comment(self, slug, entry, text, by, op_id):
        self.commented.append((slug, entry, text, by))
        self.ids.append(op_id)


@pytest.fixture
def swarm(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 1, 1))
    for agent in (ENG, BOSS):
        store.put_agent(SLUG, agent)
        store.seats.occupy(agent.seat, agent.name, 1)
    inbox = InboxStore(store.redis)
    monkeypatch.setattr(cli, "connect", lambda: inbox)
    monkeypatch.setattr(cli, "registered_name", lambda: "")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return store, inbox


def operator_write(rev, kind, target, text):
    return {"rev": rev, "at": rev, "by": "operator", "kind": kind, "target": target, "id": f"x-{rev}", "text": text}


def relayed(swarm, event):
    store, inbox = swarm
    [item] = operator_mail.relay(inbox, store, SLUG, DOC, [event], line)
    return item


def shown(swarm):
    store, inbox = swarm
    ledger = PageLedger()
    delivery.relay_to_page(inbox, SLUG, store.agents(SLUG), ledger)
    return ledger


def answer(swarm, monkeypatch, event, agent, words):
    item = relayed(swarm, event)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", agent.name)
    assert cli.main(["reply", item.id, *words]) == 0
    return item, shown(swarm)


def test_a_reply_to_an_operator_task_comment_lands_as_a_comment_on_that_task(swarm, monkeypatch):
    event = operator_write(5, "comment added", "tasks/t1", "is the fix merged")
    item, ledger = answer(swarm, monkeypatch, event, ENG, ["merged", "and", "deployed"])
    assert ledger.commented == [(SLUG, "tasks/t1", "merged and deployed", ENG.name)]
    assert ledger.said == []
    _, inbox = swarm
    assert inbox.get(item.id).state == "done" and inbox.pending_items("operator") == []
    assert ledger.ids == [inbox.get(item.id).reason.rpartition(" ")[2]]


def test_a_reply_to_an_operator_note_lands_as_a_comment_on_that_note(swarm, monkeypatch):
    event = operator_write(6, "note added", "", "keep the page quiet tonight")
    item, ledger = answer(swarm, monkeypatch, event, BOSS, ["understood,", "holding", "page", "changes"])
    assert ledger.commented == [(SLUG, "notes/x-6", "understood, holding page changes", BOSS.name)]
    assert ledger.said == []
    _, inbox = swarm
    assert inbox.get(item.id).state == "done"
    assert inbox.entry_of("no-such-item") == ""


def test_a_channel_reply_to_an_operator_note_lands_as_a_comment_on_that_note(swarm):
    item = relayed(swarm, operator_write(9, "note added", "", "pause the page work"))
    _, inbox = swarm
    assert channel.answer(inbox, BOSS.name, item.id, "paused until you say").startswith("sent to operator")
    ledger = shown(swarm)
    assert ledger.commented == [(SLUG, "notes/x-9", "paused until you say", BOSS.name)] and ledger.said == []


def test_a_reply_on_an_entry_is_held_to_the_comment_word_limit_and_the_item_stays_open(swarm, monkeypatch):
    item = relayed(swarm, operator_write(10, "comment added", "tasks/t1", "is the fix merged"))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", ENG.name)
    assert cli.main(["reply", item.id, SIXTY_WORDS]) == 1
    _, inbox = swarm
    assert inbox.get(item.id).state != "done" and shown(swarm).commented == []


def test_an_empty_reply_is_refused_before_its_answer_is_tied_to_the_entry(swarm):
    item = relayed(swarm, operator_write(13, "comment added", "tasks/t1", "is the fix merged"))
    _, inbox = swarm
    before = set(inbox.redis.scan_iter(match=inbox.key("entry", "*")))
    with pytest.raises(InboxError, match="^a reply needs text$"):
        inbox.reply(item.id, ENG.name, "  ")
    assert set(inbox.redis.scan_iter(match=inbox.key("entry", "*"))) == before
    assert inbox.get(item.id).state == "pending"


def test_a_reply_to_a_chat_line_keeps_the_chat_word_limit(swarm, monkeypatch):
    _, ledger = answer(swarm, monkeypatch, operator_write(11, "message added", "chat", "status"), BOSS, [SIXTY_WORDS])
    assert ledger.said == [(SLUG, SIXTY_WORDS, BOSS.name)]


def test_an_item_entry_is_listed_with_its_keys_and_others_are_not(swarm):
    _, inbox = swarm
    plain = inbox.send("operator", "eng", "hello")
    item = relayed(swarm, operator_write(12, "comment added", "tasks/t1", "is the fix merged"))
    keys, _ = inbox.keys_for(lambda address: True)
    assert inbox.key("entry", item.id) in keys and inbox.key("entry", plain.id) not in keys


@pytest.mark.parametrize(
    ("kind", "target", "entry"),
    [
        ("comment added", "notes/n2", "notes/n2"),
        ("comment added", "phases/p1", "phases/p1"),
        ("answer added", "questions/q1", "questions/q1"),
        ("comment added", "followups/f1", "followups/f1"),
        ("checked", "phases/p2", "phases/p2"),
    ],
)
def test_a_reply_to_any_operator_entry_write_lands_on_that_entry(swarm, monkeypatch, kind, target, entry):
    _, ledger = answer(swarm, monkeypatch, operator_write(7, kind, target, "look here"), BOSS, ["on", "it"])
    assert ledger.commented == [(SLUG, entry, "on it", BOSS.name)] and ledger.said == []


@pytest.mark.parametrize(
    ("kind", "target"),
    [("message added", "chat"), ("title changed", "title"), ("stats sync requested", "")],
)
def test_a_reply_to_a_chat_line_or_a_write_without_an_entry_still_answers_in_chat(swarm, monkeypatch, kind, target):
    _, ledger = answer(swarm, monkeypatch, operator_write(8, kind, target, "status please"), BOSS, ["all", "green"])
    assert ledger.said == [(SLUG, "all green", BOSS.name)] and ledger.commented == []


@pytest.mark.parametrize(
    ("event", "entry"),
    [
        ({"kind": "note edited", "target": "", "id": "n-7"}, "notes/n-7"),
        ({"kind": "comment added", "target": "tasks"}, ""),
        ({"kind": "comment added", "target": "artifacts/a1"}, ""),
        ({"kind": "checked", "target": "tasks/t9"}, "tasks/t9"),
        ({"kind": "checked", "target": "tasks/t9/done"}, "tasks/t9"),
    ],
)
def test_the_entry_of_an_operator_write_is_its_list_and_id(event, entry):
    assert operator_mail.entry(event) == entry


def test_relay_comment_posts_an_answer_to_the_operator_on_the_entry_with_the_service_credential(monkeypatch):
    calls = []

    def call(slug, ops, service):
        calls.append((slug, ops, service))
        return {"rejected": []}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call))
    client = ledger_client.LedgerClient()
    client.relay_comment("demo", "notes/n-1", "holding page changes", "master@a1b2c3-0002", "a1b2c3d4e5f6")
    [(slug, [op], service)] = calls
    assert (slug, service) == ("demo", True)
    assert op == {
        "op": "add",
        "id": "a1b2c3d4e5f6",
        "by": "master@a1b2c3-0002",
        "thread": "notes/n-1/comments",
        "text": "holding page changes",
        "to": "operator",
    }
