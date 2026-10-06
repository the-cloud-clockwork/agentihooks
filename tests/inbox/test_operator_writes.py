import pytest

import hooks.context.inbox_delivery as inbox_delivery
from scripts.inbox import wake
from scripts.inbox.seen import SeenMarks, first_showing, write_ref
from scripts.inbox.store import InboxStore
from scripts.swarm import operator_mail
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger.watch_ledger import line

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "sw"
MASTER_SEAT = f"master@{SLUG}"
ENG = AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1", seat=f"eng-1@{SLUG}")
CI = AgentRecord("sw-ci-1", "ci", "t2", pane_id="p2", seat=f"ci-1@{SLUG}")
BOSS = AgentRecord("sw-master-1", MASTER, MASTER, pane_id="pm", seat=MASTER_SEAT)
DOC = {
    "tasks": [
        {"id": "t1", "state": "claimed", "claimed_by": "sw-eng-1"},
        {"id": "t9", "state": "claimed", "claimed_by": "sw-eng-7"},
    ]
}


class FakeHerdr:
    def __init__(self, status):
        self.status, self.prompts = dict(status), []

    def agent_status(self, agent):
        return self.status.get(agent.pane_id, "unknown")

    def prompt(self, agent, text):
        self.prompts.append((agent.pane_id, text))


@pytest.fixture
def swarm():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 1, 1))
    for agent in (ENG, CI, BOSS):
        store.put_agent(SLUG, agent)
        store.seats.occupy(agent.seat, agent.name, 1)
    return store, InboxStore(store.redis)


def write(rev, target, kind="comment added", text="please look", **extra):
    return {
        "rev": rev,
        "at": rev,
        "by": "operator",
        "kind": kind,
        "target": target,
        "id": f"c-{rev}",
        "text": text,
        **extra,
    }


def relay(swarm, *events):
    store, inbox = swarm
    return operator_mail.relay(inbox, store, SLUG, DOC, list(events), line)


def pending(inbox, address):
    return [(i.sender, i.text, i.ref) for i in inbox.pending_items(address)]


def test_an_operator_reply_on_a_claimed_tasks_comment_is_one_item_for_that_tasks_agent(swarm):
    _, inbox = swarm
    reply = write(5, "tasks/t1/comments/c-1", kind="reply added", text="yes, ship it")
    relay(swarm, reply)
    [(sender, text, ref)] = pending(inbox, ENG.seat)
    assert sender == "operator" and "yes, ship it" in text and ref == write_ref(SLUG, reply)
    assert pending(inbox, MASTER_SEAT) == [] and pending(inbox, CI.seat) == []


def test_a_reply_on_a_phase_goes_to_the_master(swarm):
    _, inbox = swarm
    relay(swarm, write(5, "phases/p1/comments/c-1", kind="reply added"))
    assert len(pending(inbox, MASTER_SEAT)) == 1 and pending(inbox, ENG.seat) == []


@pytest.mark.parametrize(
    "event",
    [
        write(5, "questions/q1", kind="answer added"),
        write(5, "notes", kind="note added"),
        write(5, "phases/p2", kind="checked", text=None),
        write(5, "followups/f1", kind="unchecked", text=None),
        write(5, "", kind="stats sync requested", text="Operator stats sync."),
        write(5, "title", kind="title changed", text="New title"),
    ],
)
def test_answers_notes_and_checks_go_to_the_master(swarm, event):
    _, inbox = swarm
    relay(swarm, {k: v for k, v in event.items() if v is not None})
    assert len(pending(inbox, MASTER_SEAT)) == 1


def test_a_sync_order_is_one_item_for_every_live_agent(swarm):
    store, inbox = swarm
    store.put_agent(SLUG, AgentRecord("sw-eng-2", "eng", "t3", state="finished", seat=f"eng-2@{SLUG}"))
    relay(swarm, write(5, "", kind="sync requested", text="Operator sync."))
    assert [len(pending(inbox, a.seat)) for a in (ENG, CI, BOSS)] == [1, 1, 1]
    assert pending(inbox, f"eng-2@{SLUG}") == []


def test_a_check_on_a_task_goes_to_its_agent(swarm):
    _, inbox = swarm
    relay(swarm, {"rev": 5, "at": 5, "by": "operator", "kind": "checked", "target": "tasks/t1"})
    assert len(pending(inbox, ENG.seat)) == 1


def test_a_chat_line_goes_to_its_addressee_and_an_unaddressed_one_to_the_master(swarm):
    _, inbox = swarm
    relay(
        swarm,
        write(5, "chat", kind="message added", text="@sw-ci-1 the slow job"),
        write(6, "chat", kind="message added", text="how far along"),
    )
    [(_, text, _)] = pending(inbox, CI.seat)
    assert text == f"On ledger {SLUG}: " + line(write(5, "chat", kind="message added", text="@sw-ci-1 the slow job"))
    assert len(pending(inbox, MASTER_SEAT)) == 1


def test_a_chat_line_to_the_swarm_is_one_item_for_every_live_agent_with_its_images(swarm):
    store, inbox = swarm
    store.put_agent(SLUG, AgentRecord("sw-eng-2", "eng", "t3", state="finished", seat=f"eng-2@{SLUG}"))
    to_all = write(5, "chat", kind="message added", text="@swarm stop and sync", image_paths=["/media/shot.png"])
    relay(swarm, to_all)
    items = [pending(inbox, a.seat) for a in (ENG, CI, BOSS)]
    assert [len(got) for got in items] == [1, 1, 1]
    assert {got[0][1] for got in items} == {f"On ledger {SLUG}: " + line(to_all)}
    assert "/media/shot.png" in items[0][0][1]
    assert pending(inbox, f"eng-2@{SLUG}") == []


def test_a_write_for_an_agent_that_is_gone_goes_to_the_master(swarm):
    _, inbox = swarm
    relay(swarm, write(5, "tasks/t9"), write(6, "chat", kind="message added", text="@sw-eng-7 hello"))
    assert len(pending(inbox, MASTER_SEAT)) == 2


def test_a_live_master_without_a_seat_gets_the_item_by_name(swarm):
    store, inbox = swarm
    store.put_agent(SLUG, AgentRecord(BOSS.name, MASTER, MASTER, pane_id="pm"))
    relay(swarm, write(5, "phases/p1"))
    assert pending(inbox, MASTER_SEAT) == [] and len(pending(inbox, BOSS.name)) == 1


def test_agent_writes_and_cleared_chat_are_not_relayed(swarm):
    _, inbox = swarm
    relay(swarm, {**write(5, "tasks/t1"), "by": "sw-eng-1"}, write(6, "chat", kind="chat cleared"))
    assert inbox.pending() == []


def test_a_ledger_without_a_swarm_sends_nothing(swarm):
    store, inbox = swarm
    assert operator_mail.relay(inbox, store, "other", DOC, [write(5, "phases/p1")], line) == []
    assert inbox.pending() == []


def test_the_same_write_relayed_twice_is_one_item_and_two_writes_are_two(swarm):
    _, inbox = swarm
    relay(swarm, write(5, "tasks/t1"))
    relay(swarm, write(5, "tasks/t1"))
    assert len(pending(inbox, ENG.seat)) == 1
    relay(swarm, write(6, "tasks/t1"))
    assert len(pending(inbox, ENG.seat)) == 2


def test_an_idle_pane_is_woken_for_an_operator_write_with_no_tool_call(swarm):
    store, inbox = swarm
    relay(swarm, write(5, "tasks/t1"))
    herdr = FakeHerdr({"p1": "idle"})
    item = inbox.pending_items(ENG.seat)[0]
    wake.wake_pass(inbox, SLUG, store.agents(SLUG), herdr, None, item.created_at + 1, 300_000)
    assert herdr.prompts == [("p1", wake.WAKE_TEXT)]


def test_a_write_already_shown_by_the_ledger_watch_is_closed_instead_of_woken(swarm):
    store, inbox = swarm
    event = write(5, "tasks/t1")
    relay(swarm, event)
    assert first_showing(SeenMarks(inbox.redis), ENG.name, SLUG, [event]) == [event]
    herdr = FakeHerdr({"p1": "idle"})
    item = inbox.pending_items(ENG.seat)[0]
    wake.wake_pass(inbox, SLUG, store.agents(SLUG), herdr, None, item.created_at + 1, 300_000)
    assert herdr.prompts == [] and inbox.get(item.id).state == "done"


@pytest.fixture
def as_eng(swarm, monkeypatch):
    _, inbox = swarm
    monkeypatch.setattr(inbox_delivery, "connect", lambda environ=None: inbox)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", ENG.name)
    return inbox


def test_the_same_write_shown_by_the_ledger_hook_then_the_inbox_reaches_the_agent_once(swarm, as_eng):
    event = write(5, "tasks/t1", text="rename the flag")
    relay(swarm, event)
    marks = SeenMarks(as_eng.redis)
    assert first_showing(marks, ENG.name, SLUG, [event]) == [event]
    assert inbox_delivery.pending_context("s1") == ""
    assert as_eng.pending_items(ENG.seat) == []
    assert first_showing(marks, ENG.name, SLUG, [event]) == []


def test_the_inbox_first_then_the_ledger_hook_skips_it(swarm, as_eng):
    event = write(5, "tasks/t1", text="rename the flag")
    relay(swarm, event)
    assert "rename the flag" in inbox_delivery.pending_context("s1")
    assert first_showing(SeenMarks(as_eng.redis), ENG.name, SLUG, [event]) == []


def test_two_writes_reach_the_agent_as_two_items(swarm, as_eng):
    relay(swarm, write(5, "tasks/t1", text="first"), write(6, "tasks/t1", text="second"))
    shown = inbox_delivery.pending_context("s1")
    assert shown.count("=== INBOX") == 2 and "first" in shown and "second" in shown


def test_seen_marks_fail_open_without_a_store():
    events = [write(5, "tasks/t1")]
    assert first_showing(None, ENG.name, SLUG, events) == events
