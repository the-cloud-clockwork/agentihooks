import threading

import pytest

from scripts.inbox import exits
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore

SLUG = "sw"
HARNESSES = ["claude", "codex"]
pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def server():
    import fakeredis

    return fakeredis.FakeServer()


def client(server):
    import fakeredis

    return fakeredis.FakeRedis(server=server, decode_responses=True)


def crew(redis, harness):
    store, inbox = RedisStore(redis), InboxStore(redis)
    store.names.mint_code(SLUG, SLUG, "/repo")
    master = store.names.next(SLUG, "master")
    store.seats.occupy(f"master@{SLUG}", master, 1)
    a = store.names.next(SLUG, "eng")
    b = store.names.next(SLUG, "eng")
    store.seats.occupy(f"eng-1@{SLUG}", a, 1)
    store.seats.occupy(f"eng-2@{SLUG}", b, 1)
    for name, lane, task, seat in (
        (master, "master", "", f"master@{SLUG}"),
        (a, "eng", "tA", f"eng-1@{SLUG}"),
        (b, "eng", "tB", f"eng-2@{SLUG}"),
    ):
        store.put_agent(SLUG, AgentRecord(name=name, lane=lane, task=task, harness=harness, seat=seat))
    return store, inbox, master, a, b


def retire(store, name, at=5):
    store.put_agent(SLUG, AgentRecord(name=name, lane="eng", task="tA", state="finished"))
    store.drop_agent(SLUG, name, at=at)


ROWS = {"tA": {"claimed_by": "", "state": "done"}, "tB": {"claimed_by": "", "state": "claimed"}}


@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.parametrize("order", ["reply-before-exit", "reply-after-exit"])
def test_a_reply_to_an_agent_that_finished_its_task_never_reaches_another_tasks_agent(server, harness, order):
    store, inbox, master, a, b = crew(client(server), harness)
    ROWS["tB"]["claimed_by"] = b
    question = inbox.send(a, master, "is the gate red on dev?")
    if order == "reply-before-exit":
        answer = inbox.reply(question.id, master, "yes, rebase")
        retire(store, a)
        exits.settle(inbox, a, "", "finished its task and exited")
    else:
        retire(store, a)
        exits.settle(inbox, a, "", "finished its task and exited")
        answer = inbox.reply(question.id, master, "yes, rebase")
    exits.sweep(inbox, SLUG, store, lambda: ROWS)
    landed = inbox.get(answer.id)
    assert landed.address != b, f"reply for {a} (task tA) landed at {b} (task tB): {landed.reason}"
    assert (landed.address, landed.state) == (a, "cancelled")
    assert any(answer.id in item.text for item in inbox.pending_items(master))


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_reply_after_a_same_task_handoff_reaches_the_seat_successor_not_another_lane(server, harness):
    store, inbox, master, a, b = crew(client(server), harness)
    question = inbox.send(a, master, "which base for the pull request?")
    store.put_agent(SLUG, AgentRecord(name=a, lane="eng", task="tA", state="finished", seat=f"eng-1@{SLUG}"))
    exits.settle(inbox, a, f"eng-1@{SLUG}", "handed off its seat")
    retire(store, a)
    c = store.names.next(SLUG, "eng")
    store.seats.occupy(f"eng-1@{SLUG}", c, 6)
    store.put_agent(SLUG, AgentRecord(name=c, lane="eng", task="tA", harness=harness, seat=f"eng-1@{SLUG}"))
    answer = inbox.reply(question.id, master, "dev")
    exits.sweep(inbox, SLUG, store, lambda: {"tA": {"claimed_by": c, "state": "claimed"}, "tB": {"claimed_by": b}})
    landed = inbox.get(answer.id)
    assert landed.address != b, f"handoff reply for task tA landed at {b} (task tB): {landed.reason}"
    assert [i.id for i in inbox.mailbox(c)] == [answer.id]


@pytest.mark.parametrize("harness", HARNESSES)
def test_control_a_handoff_with_no_other_engineer_keeps_seat_continuity(server, harness):
    redis = client(server)
    store, inbox = RedisStore(redis), InboxStore(redis)
    store.names.mint_code(SLUG, SLUG, "/repo")
    master, a = store.names.next(SLUG, "master"), store.names.next(SLUG, "eng")
    store.seats.occupy(f"eng-1@{SLUG}", a, 1)
    question = inbox.send(a, master, "which base?")
    exits.settle(inbox, a, f"eng-1@{SLUG}", "handed off its seat")
    retire(store, a)
    c = store.names.next(SLUG, "eng")
    store.seats.occupy(f"eng-1@{SLUG}", c, 6)
    store.put_agent(SLUG, AgentRecord(name=c, lane="eng", task="tA", harness=harness, seat=f"eng-1@{SLUG}"))
    answer = inbox.reply(question.id, master, "dev")
    exits.sweep(inbox, SLUG, store, lambda: {"tA": {"claimed_by": c, "state": "claimed"}})
    assert [i.id for i in inbox.mailbox(c)] == [answer.id]


@pytest.mark.parametrize("harness", HARNESSES)
def test_race_reply_against_retirement_repeated(server, harness):
    misrouted = []
    for round_ in range(25):
        server = __import__("fakeredis").FakeServer()
        store, inbox, master, a, b = crew(client(server), harness)
        question = inbox.send(a, master, f"round {round_}")
        go = threading.Barrier(2)
        out = {}

        def answer():
            go.wait()
            out["id"] = InboxStore(client(server)).reply(question.id, master, "answer").id

        def leave():
            go.wait()
            retire(RedisStore(client(server)), a)
            exits.settle(InboxStore(client(server)), a, "", "finished its task and exited")

        threads = [threading.Thread(target=answer), threading.Thread(target=leave)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        exits.sweep(inbox, SLUG, store, lambda: {"tA": {"state": "done"}, "tB": {"claimed_by": b, "state": "claimed"}})
        if inbox.get(out["id"]).address == b:
            misrouted.append(round_)
    assert misrouted == [], f"{len(misrouted)} of 25 races delivered the reply to the other task's agent"
