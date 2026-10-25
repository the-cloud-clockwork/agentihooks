import json
from dataclasses import replace

import pytest

from scripts.doctor import handoffs, read
from scripts.inbox.store import InboxStore
from scripts.swarm.keyspace import ROOT as KEY_ROOT
from scripts.swarm.store import RedisStore

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


def test_one_inbox_read_of_a_large_inbox_takes_a_bounded_number_of_round_trips(redis, monkeypatch):
    from dataclasses import asdict

    from redis.client import Pipeline

    from scripts.inbox.seats import of_swarm

    box, sent = InboxStore(redis), []
    for n in range(100):
        for k in range(3):
            sent.append(box.send(f"{SLUG}-eng-{k}", f"other-{n}", f"note {n} {k}").id)
        box.close(sent[-1], f"other-{n}", "done", "answered in the thread")
    for n in range(30):
        box.send("operator", f"stranger-{n}", "not ours")
    prefix = box.key("address", "")
    expected = [
        {**asdict(item), "history": box.history(item.id)}
        for key in sorted(redis.scan_iter(match=prefix + "*"))
        for item in box.inbox(key[len(prefix) :])
        if of_swarm(item.address, SLUG, box.names) or of_swarm(item.sender, SLUG, box.names)
    ]
    trips = []
    direct, batched = redis.execute_command, Pipeline.execute
    monkeypatch.setattr(redis, "execute_command", lambda *a, **k: trips.append(a[0]) or direct(*a, **k))
    monkeypatch.setattr(Pipeline, "execute", lambda pipe, *a, **k: trips.append("pipeline") or batched(pipe, *a, **k))
    rows = read.inbox_items(box, SLUG)
    assert rows == expected
    assert sorted(row["id"] for row in rows) == sorted(sent)
    assert sum(len(row["history"]) for row in rows) == len(sent) + 100
    assert len(trips) <= 12


def test_an_inbox_read_in_small_batches_keeps_every_batch_and_registry_named_agents(redis, monkeypatch):
    from scripts.swarm.store import SwarmConfig

    store, box = RedisStore(redis), InboxStore(redis)
    store.create(SwarmConfig(SLUG, "/home/x/dev/r", max_eng=1, max_ci=1))
    named, other = store.next_name(SLUG, "eng"), "stranger-9"
    to_named = [box.send("operator", named, f"to the named agent {n}").id for n in range(3)]
    from_named = [box.send(named, f"other-{n}", f"from the named agent {n}").id for n in range(3)]
    box.send("operator", other, "not ours")
    box.deliver(to_named[0], named)
    box.read(to_named[0], named)
    box.close(to_named[0], named, "done", "answered in the thread")
    monkeypatch.setattr(read, "BATCH", 2)
    rows = read.inbox_items(box, SLUG)
    assert sorted(row["id"] for row in rows) == sorted(to_named + from_named)
    assert {row["id"]: row["history"] for row in rows} == {row["id"]: box.history(row["id"]) for row in rows}
    assert max(len(row["history"]) for row in rows) >= 3


def test_an_inbox_listing_whose_item_is_gone_is_refused(redis):
    from scripts.inbox.store import InboxError

    box = InboxStore(redis)
    box.send(f"{SLUG}-eng-1", "operator", "kept")
    redis.zadd(box.key("address", "operator"), {"gone": 1})
    with pytest.raises(InboxError, match="no message gone"):
        read.inbox_items(box, SLUG)


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
        seat: Receiver(live=True, quiet_ms=2 * 60_000, occupant=f"{SLUG}-eng-1", task="t1", bound_at=10),
        named: Receiver(live=True, occupant=named, task="t6"),
        f"{SLUG}-eng-2": Receiver(),
        f"{SLUG}-eng-5": Receiver(),
        "engineer-100001-0001-tmp-1": Receiver(scoped=False),
        "operator": Receiver(),
        doctor: Receiver(seat_wait=True),
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
        seat: Receiver(live=True, occupant=f"{SLUG}-eng-2", task="t2", bound_at=10),
        f"{SLUG}-eng-1": Receiver(),
    }


def test_inbox_receivers_carry_when_the_live_occupant_bound_its_seat(redis):
    from scripts.swarm.store import AgentRecord

    store, box = RedisStore(redis), InboxStore(redis)
    seat = f"eng-1@{SLUG}"
    store.seats.occupy(seat, f"{SLUG}-eng-1", 10)
    store.seats.occupy(seat, f"{SLUG}-eng-2", 40)
    store.seats.note(seat, "handoff", "noted", 70)
    store.put_agent(SLUG, AgentRecord(name=f"{SLUG}-eng-2", lane="eng", task="t2"))
    receivers = read.inbox_receivers(store, box, SLUG, [{"address": seat, "history": []}])
    assert receivers[seat].bound_at == 40


@pytest.mark.parametrize("count", [10, 200])
def test_receiver_reads_have_constant_round_trips_and_preserve_results(redis, monkeypatch, count):
    from redis.client import Pipeline

    from scripts.doctor.inbox import Receiver
    from scripts.swarm.store import AgentRecord, SwarmConfig

    store, box = RedisStore(redis), InboxStore(redis)
    store.names.mint_code(SLUG, SLUG, "/r")
    expected, items = {}, []
    for n in range(count):
        name = store.names.next(SLUG, "eng")
        alias = f"old-{n}"
        redis.set(store.names.key("alias", alias), name)
        store.put_agent(SLUG, AgentRecord(name=name, lane="eng", task="t1", idle_ticks=n))
        seat = f"eng-{n}@{SLUG}"
        store.seats.occupy(seat, name, 10)
        foreign = f"foreign-{n}"
        store.create(SwarmConfig(foreign, "/r", 1, 1, "running" if n % 2 else "stopped"))
        taker = f"eng-1@{foreign}"
        items.extend(
            [
                {"address": alias, "history": [{"state": "delivered", "by": taker}]},
                {"address": seat, "history": []},
            ]
        )
        expected[alias] = Receiver(live=True, quiet_ms=n * 60_000, occupant=name, task="t1")
        expected[seat] = replace(expected[alias], bound_at=10)
        expected[taker] = Receiver(seat_wait=bool(n % 2))
    items.extend([{"address": "operator", "history": []}, {"address": "stranger", "history": []}])
    expected.update(operator=Receiver(), stranger=Receiver(scoped=False))
    trips = []
    direct, batched = redis.execute_command, Pipeline.execute
    monkeypatch.setattr(redis, "execute_command", lambda *a, **k: trips.append(a[0]) or direct(*a, **k))
    monkeypatch.setattr(Pipeline, "execute", lambda pipe, *a, **k: trips.append("pipeline") or batched(pipe, *a, **k))
    assert read.inbox_receivers(store, box, SLUG, items) == expected
    assert len(trips) <= 16


@pytest.mark.parametrize("state, cap, waiting", [("running", 2, True), ("stopped", 2, False), ("running", 0, False)])
def test_receiver_reads_keep_delivery_seats_and_exited_occupants(redis, state, cap, waiting):
    from scripts.doctor.inbox import Receiver
    from scripts.swarm.store import AgentRecord, SwarmConfig

    store, box = RedisStore(redis), InboxStore(redis)
    store.create(SwarmConfig(SLUG, "/r", cap, 1, state))
    name, seat, taker = f"{SLUG}-eng-1", f"eng-1@{SLUG}", f"eng-2@{SLUG}"
    store.put_agent(SLUG, AgentRecord(name=name, lane="eng", task="t1", harness="codex"))
    store.seats.occupy(seat, name, 10)
    store.seats.record_exit(name, seat, "left")
    items = [
        {"address": seat, "history": [{"state": "delivered", "by": taker}]},
        {"address": seat, "history": []},
        {"address": name, "history": []},
    ]
    assert read.inbox_receivers(store, box, SLUG, items) == {
        seat: Receiver(seat_wait=waiting),
        taker: Receiver(seat_wait=waiting),
        name: Receiver(live=True, occupant=name, harness="codex", task="t1"),
    }


@pytest.mark.parametrize("plan_cap", [None, 0, 2])
def test_receiver_reads_operator_delivery_seats_and_legacy_planner_limits(redis, plan_cap):
    from scripts.doctor.inbox import Receiver
    from scripts.swarm.store import SwarmConfig

    store, box = RedisStore(redis), InboxStore(redis)
    for slug in (SLUG, "foreign"):
        store.create(SwarmConfig(slug, "/r", 1, 1, "running", max_plan=plan_cap or 0))
        if plan_cap is None:
            redis.hdel(store.key(slug, "config"), "max_plan")
    first, second = "plan-1@foreign", f"plan-2@{SLUG}"
    items = [
        {"address": "operator", "history": [{"state": "delivered", "by": first}]},
        {"address": second, "history": []},
    ]
    assert read.inbox_receivers(store, box, SLUG, items) == {
        "operator": Receiver(),
        first: Receiver(seat_wait=plan_cap != 0),
        second: Receiver(seat_wait=plan_cap == 2),
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


@pytest.mark.parametrize("batch, maximum_scans", [(100, 30), (1000, 5)])
def test_recorded_pending_handoffs_keep_findings_with_bounded_scan_pages(
    redis, tmp_path, monkeypatch, batch, maximum_scans
):
    from pathlib import Path

    recorded = json.loads((Path(__file__).parents[1] / "fixtures/doctor/handoffs.json").read_text())
    store, box = RedisStore(redis), InboxStore(redis)
    expected = []
    seeded = set()
    for index, handoff in enumerate(recorded["handoffs"]):
        task = f"recorded-{index}"
        store.put_handoff(SLUG, task, handoff["document"], handoff["seat"], {"agent": handoff["from"]})
        if handoff["seat"] not in seeded:
            for recap in reversed(handoff["recaps"]):
                store.memory.add_recap(handoff["seat"], recap["occupant"], recap["task"], recap["text"], recap["at"])
            seeded.add(handoff["seat"])
        expected.append({**handoff, "task": task, "to": "", "at": 0, "asked": []})
    redis.mset({f"unrelated:{index}": "value" for index in range(2500)})
    store.put_handoff("other", "not-ours", "ignored")
    scans = []
    scan = redis.scan
    monkeypatch.setattr(redis, "scan", lambda *a, **k: scans.append(1) or scan(*a, **k))
    monkeypatch.setattr(read, "BATCH", batch)
    rows = read.handoffs(store, box, tmp_path, SLUG, items=[])
    assert rows == expected
    assert handoffs.findings(rows) == handoffs.findings(expected)
    assert [finding.id for finding in handoffs.findings(rows)] == ["missing-recap/master@rig-grade-swarm"]
    assert len(scans) <= maximum_scans
