import json

import pytest

from scripts.doctor import handoffs, read
from scripts.inbox.store import InboxStore
from scripts.swarm.keyspace import ROOT as KEY_ROOT
from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "sw"


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def test_health_records_are_read_from_the_swarm_findings(redis):
    record = {"seen_at": 5, "verdict": None, "returned": False, "evidence": ["e"], "measure": 3}
    redis.hset(f"{KEY_ROOT}:swarm:{SLUG}:findings", "stale-claim/t1", json.dumps(record))
    redis.hset(f"{KEY_ROOT}:swarm:other:findings", "stale-claim/t9", json.dumps(record))
    assert read.health_records(redis, SLUG) == {"stale-claim/t1": record}


def test_inbox_items_of_the_swarm_carry_their_history(redis):
    box = InboxStore(redis)
    kept = box.send("operator", f"eng-1@{SLUG}", "look at this")
    reply = box.send(f"{SLUG}-eng-3", "operator", "done it")
    box.send("operator", "other-eng-1", "not ours")
    box.close(kept.id, f"eng-1@{SLUG}", "done", "reviewed the request")
    rows = {row["id"]: row for row in read.inbox_items(box, SLUG)}
    assert set(rows) == {kept.id, reply.id}
    assert rows[kept.id]["state"] == "done"
    assert [e["state"] for e in rows[kept.id]["history"]] == ["pending", "done"]


def test_inbox_receivers_name_who_is_live_how_long_quiet_and_who_sits_outside_the_swarm(redis):
    from scripts.doctor.inbox import Receiver
    from scripts.doctor.priming import doctor_slug
    from scripts.swarm.store import AgentRecord

    store, box = RedisStore(redis), InboxStore(redis)
    seat, doctor = f"eng-1@{SLUG}", f"master@{doctor_slug(SLUG)}"
    store.seats.occupy(seat, f"{SLUG}-eng-1", 10)
    store.put_agent(SLUG, AgentRecord(name=f"{SLUG}-eng-1", lane="eng", task="t1", idle_ticks=2))
    store.put_agent(SLUG, AgentRecord(name=f"{SLUG}-eng-5", lane="eng", task="t5", state="finished"))
    store.names.mint_code(SLUG, SLUG, "/r")
    named = store.names.next(SLUG, "eng")
    store.put_agent(SLUG, AgentRecord(name=named, lane="eng", task="t6"))
    addresses = [seat, named, f"{SLUG}-eng-2", f"{SLUG}-eng-5", "engineer-100001-0001-tmp-1", "operator", doctor]
    items = [{"address": address, "history": []} for address in addresses]
    assert read.inbox_receivers(store, box, SLUG, items) == {
        seat: Receiver(live=True, quiet_ms=2 * 60_000),
        named: Receiver(live=True),
        f"{SLUG}-eng-2": Receiver(),
        f"{SLUG}-eng-5": Receiver(),
        "engineer-100001-0001-tmp-1": Receiver(scoped=False),
        "operator": Receiver(),
        doctor: Receiver(),
    }


def test_inbox_receivers_name_the_agent_that_took_delivery_apart_from_the_seat_occupant(redis):
    from scripts.doctor.inbox import Receiver
    from scripts.swarm.store import AgentRecord

    store, box = RedisStore(redis), InboxStore(redis)
    seat = f"eng-1@{SLUG}"
    store.seats.occupy(seat, f"{SLUG}-eng-2", 10)
    store.put_agent(SLUG, AgentRecord(name=f"{SLUG}-eng-2", lane="eng", task="t2"))
    store.put_agent(SLUG, AgentRecord(name=f"{SLUG}-eng-1", lane="eng", task="t1", state="finished"))
    took = [{"state": "pending", "by": "swarm"}, {"state": "delivered", "by": f"{SLUG}-eng-1"}]
    items = [{"address": seat, "history": took}, {"address": seat, "history": took[:1]}]
    assert read.inbox_receivers(store, box, SLUG, items) == {
        seat: Receiver(live=True),
        f"{SLUG}-eng-1": Receiver(),
    }


def prompt(name, seat, task, handoff):
    lines = [f"You are {name}, an engineer in swarm {SLUG}.", f"Your one task for this session is {task}: a title"]
    lines.append(f"Your seat {seat} carries what earlier occupants left. Read it in this order:")
    lines += ["1. Handoff document: a previous agent ran out of context and left it. Continue from it:", handoff]
    return "\n".join([*lines, "2. Swarm culture: none written for this swarm yet.", "rest"])


def test_handoffs_pair_the_successor_prompt_with_its_predecessor_recaps_and_questions(redis, tmp_path):
    store, box = RedisStore(redis), InboxStore(redis)
    seat = f"eng-1@{SLUG}"
    store.seats.occupy(seat, f"{SLUG}-eng-1", 10)
    store.memory.add_recap(seat, f"{SLUG}-eng-1", "t1", "did half", 15)
    store.seats.occupy(seat, f"{SLUG}-eng-4", 20)
    asked = box.send(f"{SLUG}-eng-4", f"master@{SLUG}", "which branch?")
    prompts = tmp_path / SLUG / "prompts"
    prompts.mkdir(parents=True)
    (prompts / f"{SLUG}-eng-4.md").write_text(prompt(f"{SLUG}-eng-4", seat, "t1", "# t1 handoff\nbranch b1"))
    (prompts / f"{SLUG}-eng-1.md").write_text("Your seat eng-1@sw has no history yet.")
    [record] = read.handoffs(store, box, tmp_path, SLUG)
    assert record["seat"] == seat and record["task"] == "t1"
    assert (record["from"], record["to"], record["at"]) == (f"{SLUG}-eng-1", f"{SLUG}-eng-4", 20)
    assert record["document"] == "# t1 handoff\nbranch b1"
    assert [r["text"] for r in record["recaps"]] == ["did half"]
    assert [a["id"] for a in record["asked"]] == [asked.id]


def test_a_handoff_still_waiting_for_its_successor_is_read(redis, tmp_path):
    store, box = RedisStore(redis), InboxStore(redis)
    seat = f"eng-2@{SLUG}"
    store.seats.occupy(seat, f"{SLUG}-eng-2", 10)
    store.put_handoff(SLUG, "t2", "# t2 handoff", seat=seat, envelope={"agent": f"{SLUG}-eng-2", "task": "t2"})
    [record] = read.handoffs(store, box, tmp_path, SLUG)
    assert (record["task"], record["from"], record["to"], record["at"], record["document"]) == (
        "t2",
        f"{SLUG}-eng-2",
        "",
        0,
        "# t2 handoff",
    )


def test_a_waiting_handoff_is_authored_by_its_envelope_agent_not_the_next_seat_holder(redis, tmp_path):
    store, box = RedisStore(redis), InboxStore(redis)
    seat = f"eng-2@{SLUG}"
    store.seats.occupy(seat, f"{SLUG}-eng-2", 10)
    store.memory.add_recap(seat, f"{SLUG}-eng-2", "t2", "did half", 15)
    store.put_handoff(SLUG, "t2", "# t2 handoff", seat=seat, envelope={"agent": f"{SLUG}-eng-2", "task": "t2"})
    store.seats.occupy(seat, f"{SLUG}-eng-7", 20)
    [record] = read.handoffs(store, box, tmp_path, SLUG)
    assert (record["task"], record["from"]) == ("t2", f"{SLUG}-eng-2")
    assert handoffs.missing_recap([record]) == []


def test_a_waiting_handoff_without_an_envelope_never_names_the_seat_holder(redis, tmp_path):
    store, box = RedisStore(redis), InboxStore(redis)
    seat = f"eng-2@{SLUG}"
    store.seats.occupy(seat, f"{SLUG}-eng-7", 20)
    store.put_handoff(SLUG, "t2", "# t2 handoff", seat=seat)
    [record] = read.handoffs(store, box, tmp_path, SLUG)
    assert record["from"] == ""
    [finding] = handoffs.missing_recap([record])
    assert finding.subject == seat


def test_a_waiting_handoff_whose_author_left_no_recap_is_raised_on_that_author(redis, tmp_path):
    store, box = RedisStore(redis), InboxStore(redis)
    seat = f"eng-2@{SLUG}"
    store.seats.occupy(seat, f"{SLUG}-eng-2", 10)
    store.put_handoff(SLUG, "t2", "# t2 handoff", seat=seat, envelope={"agent": f"{SLUG}-eng-2", "task": "t2"})
    store.seats.occupy(seat, f"{SLUG}-eng-7", 20)
    store.memory.add_recap(seat, f"{SLUG}-eng-7", "t9", "other task", 25)
    [finding] = handoffs.missing_recap(read.handoffs(store, box, tmp_path, SLUG))
    assert finding.subject == f"{SLUG}-eng-2"
    assert finding.evidence[:2] == (f"task t2 on seat {seat}", f"handed off by {SLUG}-eng-2")


def test_handoff_reader_reuses_supplied_empty_mail_snapshot(redis, tmp_path, monkeypatch):
    store = RedisStore(redis)
    inbox = InboxStore(redis)

    def unread(*args):
        pytest.fail("handoff reader rescanned mail")

    monkeypatch.setattr(read, "inbox_items", unread)
    assert read.handoffs(store, inbox, tmp_path, SLUG, []) == []
