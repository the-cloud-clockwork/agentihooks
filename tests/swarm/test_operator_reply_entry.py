import pytest

from scripts.inbox import cli
from scripts.inbox.store import InboxStore
from scripts.swarm import delivery, operator_mail
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger.watch_ledger import line

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "sw"
ENG = AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1", seat=f"eng-1@{SLUG}")
BOSS = AgentRecord("sw-master-1", MASTER, MASTER, pane_id="pm", seat=f"master@{SLUG}")
DOC = {"tasks": [{"id": "t1", "state": "claimed", "claimed_by": ENG.name}]}


class PageLedger:
    def __init__(self):
        self.said, self.commented = [], []

    def relay(self, slug, text, by):
        self.said.append((slug, text, by))

    def relay_comment(self, slug, entry, text, by):
        self.commented.append((slug, entry, text, by))


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


def answer(swarm, monkeypatch, event, agent, words):
    store, inbox = swarm
    [item] = operator_mail.relay(inbox, store, SLUG, DOC, [event], line)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", agent.name)
    assert cli.main(["reply", item.id, *words]) == 0
    ledger = PageLedger()
    delivery.relay_to_page(inbox, SLUG, store.agents(SLUG), ledger)
    return item, ledger


def test_a_reply_to_an_operator_task_comment_lands_as_a_comment_on_that_task(swarm, monkeypatch):
    event = operator_write(5, "comment added", "tasks/t1", "is the fix merged")
    item, ledger = answer(swarm, monkeypatch, event, ENG, ["merged", "and", "deployed"])
    assert ledger.commented == [(SLUG, "tasks/t1", "merged and deployed", ENG.name)]
    assert ledger.said == []
    _, inbox = swarm
    assert inbox.get(item.id).state == "done" and inbox.pending_items("operator") == []


def test_a_reply_to_an_operator_note_lands_as_a_comment_on_that_note(swarm, monkeypatch):
    event = operator_write(6, "note added", "", "keep the page quiet tonight")
    item, ledger = answer(swarm, monkeypatch, event, BOSS, ["understood,", "holding", "page", "changes"])
    assert ledger.commented == [(SLUG, "notes/x-6", "understood, holding page changes", BOSS.name)]
    assert ledger.said == []
    _, inbox = swarm
    assert inbox.get(item.id).state == "done"


@pytest.mark.parametrize(
    ("kind", "target", "entry"),
    [
        ("comment added", "notes/n2", "notes/n2"),
        ("comment added", "phases/p1", "phases/p1"),
        ("answer added", "questions/q1", "questions/q1"),
        ("comment added", "followups/f1/comments/c-1", "followups/f1"),
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
