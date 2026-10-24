import json

import pytest

import scripts.swarm_ledger.repository  # noqa: F401  puts the ledger folder on sys.path before ledger_core loads
from scripts.inbox import cli as inbox_cli
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import delivery, master_retire
from scripts.swarm.store import AgentRecord, SwarmConfig
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_v2 import master_fence, masters
from tests import sv2_mst04_cases as cases
from tests.sv2_mst04_cases import ENGINEER, LEAD, LEAD_NAME, SECOND, SECOND_NAME, SLUG

pytestmark = pytest.mark.unit

LOOSE = "t3"
CLAIMANT = f"eng-1@{SLUG}"
OWN_CLAIMANT = f"eng-2@{SLUG}"
OTHER_ENGINEER = "engineer@abcdef-0006"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path / "ledgers")
    core.LEDGER_DIR.mkdir()
    built = cases.World()
    built.store.names.adopt(SLUG, "abcdef", SLUG, "repo")
    core.sync(
        SLUG, ops=[{"op": "task_add", "id": "add-t3", "by": "swarm", "task": LOOSE, "title": "Loose", "lane": "eng"}]
    )
    for name, seat, task in ((ENGINEER, CLAIMANT, LOOSE), (OTHER_ENGINEER, OWN_CLAIMANT, "t2")):
        built.store.put_agent(SLUG, AgentRecord(name, "eng", task, pane_id=f"p-{name}", seat=seat))
        built.store.seats.occupy(seat, name, 1)

    def state(slug):
        if slug != SLUG:
            pytest.fail(f"read the ledger of {slug}")
        return built.doc()

    monkeypatch.setattr(master_fence, "_ledger_state", state, raising=False)
    return built


def comment(world, by, task):
    op = {"op": "add", "thread": f"tasks/{task}/comments", "id": f"c-{by}-{task}", "by": by, "text": "Cut over now"}
    return world.write(op)


def relay(world, by, task):
    op = {"op": "relay", "id": f"r-{by}-{task}", "by": by, "item": f"tasks/{task}"}
    return world.write(op | {"text": "Stage the turnover", "quote": "stage the authenticated turnover first"})


@pytest.mark.parametrize(
    ("op", "item"),
    [
        ({"op": "add", "thread": "tasks/t1/comments"}, "tasks/t1"),
        ({"op": "relay", "item": "tasks/t1"}, "tasks/t1"),
        ({"op": "relay", "item": "questions/q1"}, ""),
        ({"op": "add", "thread": "tasks/t1/notes"}, ""),
        ({"op": "add", "thread": "phases/p1/comments"}, ""),
        ({"op": "add", "thread": "chat"}, ""),
    ],
)
def test_task_comments_and_relays_are_covered(op, item):
    assert master_fence.decision(op) == item


def test_each_task_ruling_has_its_verb():
    assert [master_fence.verb({"op": kind}) for kind in ("add", "relay")] == ["comment on", "relay onto"]


def test_a_backend_master_comments_on_a_task_with_no_phase(world):
    assert comment(world, SECOND_NAME, LOOSE) == {"accepted": True, "warnings": []}
    task = next(t for t in world.doc()["tasks"] if t["id"] == LOOSE)
    assert len(task["comments"]) == 1
    assert [(r["owner"], r["seat"], r["by"]) for r in world.cross()[f"tasks/{LOOSE}"]] == [(LEAD, SECOND, SECOND_NAME)]
    assert world.refusals() == 0


def test_the_front_door_comments_on_a_task_with_no_phase(world):
    assert comment(world, LEAD_NAME, LOOSE) == {"accepted": True, "warnings": []}


def test_a_phase_owner_comments_on_its_own_phase_and_on_another(world):
    assert comment(world, SECOND_NAME, "t2") == {"accepted": True, "warnings": []}
    assert comment(world, SECOND_NAME, "t1") == {"accepted": True, "warnings": []}


def test_a_backend_master_relays_onto_a_task_with_no_phase(world):
    from hooks.context import operator_words

    for name in (SECOND_NAME, LEAD_NAME):
        operator_words.record(name, "stage the authenticated turnover first")
    assert relay(world, SECOND_NAME, LOOSE)["warnings"] == []
    assert relay(world, LEAD_NAME, LOOSE)["warnings"] == []
    assert world.refusals() == 0


def test_a_master_without_a_live_seat_cannot_comment_or_relay_naming_the_owner(world):
    assert comment(world, cases.FORMER, LOOSE)["warnings"] == [
        f"tasks/{LOOSE} belongs to {LEAD}, so {cases.FORMER} cannot comment on it"
    ]
    assert relay(world, cases.FORMER, "t1")["warnings"] == [
        f"tasks/t1 belongs to {LEAD}, so {cases.FORMER} cannot relay onto it"
    ]
    assert world.refusals() == 2


def test_an_engineer_comment_is_not_fenced(world):
    assert comment(world, ENGINEER, LOOSE) == {"accepted": True, "warnings": []}


def inbox(world):
    return InboxStore(world.redis, session_id="s1")


@pytest.fixture
def doctor_world(world):
    doctor = "watch-doctor"
    sender = "master@fedcba-0001"
    world.store.names.adopt(doctor, "fedcba", doctor, "repo")
    world.store.create(SwarmConfig(doctor, "/repo", 1, 0))
    world.store.names.next(doctor, "master")
    world.store.set_peer(SLUG, doctor)
    world.store.set_peer(doctor, SLUG)
    world.store.put_agent(doctor, AgentRecord(sender, "master", "master", seat=f"master@{doctor}"))
    world.store.seats.occupy(f"master@{doctor}", sender, 1)
    world.down(SECOND_NAME)
    masters.MasterSeats(world.redis).set_count(SLUG, 3)
    for seat, task in ((SECOND, "master-2"), (f"master-3@{SLUG}", "master-3")):
        name = world.store.names.next(SLUG, "master")
        world.store.put_agent(SLUG, AgentRecord(name, "master", task, seat=seat))
        world.store.seats.occupy(seat, name, 1)
    return world, doctor, sender


@pytest.mark.parametrize("front_door", [True, False])
@pytest.mark.parametrize("seat", [SECOND, f"master-3@{SLUG}"])
@pytest.mark.parametrize("by_name", [True, False])
def test_a_linked_doctor_master_messages_live_watched_masters(doctor_world, front_door, seat, by_name):
    world, _, sender = doctor_world
    address = world.store.seats.occupant(seat).occupant if by_name else seat
    if not front_door:
        world.down(LEAD_NAME)
        world.store.seats.occupy(LEAD, "", 2)
    record = {
        "op": "send",
        "by": sender,
        "item": "i-doctor",
        "args": {"address": address, "text": "Apply the merged fix", "fyi": False},
    }

    result = inbox_cli.apply(inbox(world), record)

    assert result["to"] == address
    assert inbox(world).get(result["id"]).text == "Apply the merged fix"
    assert world.refusals() == 0


@pytest.mark.parametrize("link", ["none", "other", "watched-only", "doctor-only", "retired", "retire-requested"])
def test_only_a_live_linked_doctor_master_can_bypass_the_claimant_fence(doctor_world, link):
    world, doctor, sender = doctor_world
    if link == "none":
        world.store.clear_peer(SLUG)
        world.store.clear_peer(doctor)
    elif link == "other":
        world.store.set_peer(SLUG, "another-doctor")
        world.store.set_peer(doctor, "another-swarm")
    elif link == "watched-only":
        world.store.clear_peer(doctor)
    elif link == "doctor-only":
        world.store.clear_peer(SLUG)
    elif link == "retired":
        world.store.seats.record_exit(sender, f"master@{doctor}", "retired")
    else:
        agent = next(a for a in world.store.agents(doctor) if a.name == sender)
        master_retire.hold(world.store, doctor, agent, "retired", True, 1)
        assert master_retire.reason(world.store, doctor, sender) == "retired"
    record = {
        "op": "send",
        "by": sender,
        "item": "i-unlinked",
        "args": {"address": SECOND, "text": "Coordinate", "fyi": False},
    }

    with pytest.raises(InboxError, match="cannot message its claimant"):
        inbox_cli.apply(inbox(world), record)

    assert inbox(world).mailbox(SECOND) == []


def test_a_linked_doctor_master_cannot_message_another_masters_task_claimant(doctor_world):
    world, _, sender = doctor_world
    record = {
        "op": "send",
        "by": sender,
        "item": "i-claimant",
        "args": {"address": CLAIMANT, "text": "Cut over now", "fyi": False},
    }

    with pytest.raises(InboxError, match="cannot message its claimant"):
        inbox_cli.apply(inbox(world), record)

    assert inbox(world).mailbox(CLAIMANT) == []


def test_a_backend_master_message_to_the_claimant_of_a_task_with_no_phase_is_refused(world):
    with pytest.raises(InboxError) as refused:
        master_fence.check_send(inbox(world), SECOND_NAME, CLAIMANT)

    assert str(refused.value) == (
        f"tasks/{LOOSE} belongs to {LEAD}, so {SECOND} cannot message its claimant; ask {LEAD} with agentihooks "
        f'swarm {SLUG} masters-channel say "<text>"'
    )
    assert world.refusals() == 1


@pytest.mark.parametrize(
    ("sender", "address"),
    [(LEAD_NAME, CLAIMANT), (SECOND_NAME, OWN_CLAIMANT), (OTHER_ENGINEER, CLAIMANT), (SECOND_NAME, LEAD)],
)
def test_owners_engineers_and_messages_to_agents_without_a_task_pass(world, sender, address):
    master_fence.check_send(inbox(world), sender, address)

    assert world.refusals() == 0


def test_after_a_backend_master_retires_the_front_door_messages_a_claimant_in_its_phase(world):
    third = f"master-3@{SLUG}"
    table = masters.MasterSeats(world.redis)
    table.set_count(SLUG, 3)
    world.seat(world.store.names.next(SLUG, "master"), third)
    [phase] = [p for p, owner in table.owners(SLUG, world.doc()).items() if owner == SECOND]
    world.down(SECOND_NAME)

    table.retire(SLUG, SECOND)

    assert table.owners(SLUG, world.doc())[phase] == LEAD
    assert table.retired(SLUG) == [SECOND]
    table.retire(SLUG, SECOND)
    assert table.retired(SLUG) == [SECOND]
    master_fence.check_send(inbox(world), LEAD_NAME, OWN_CLAIMANT)
    assert world.refusals() == 0


def test_after_a_backend_master_retires_the_items_it_decided_belong_to_the_front_door(world):
    table = masters.MasterSeats(world.redis)
    table.set_count(SLUG, 3)
    world.seat(world.store.names.next(SLUG, "master"), f"master-3@{SLUG}")
    assert world.decide(SECOND_NAME)["accepted"]
    world.down(SECOND_NAME)

    table.retire(SLUG, SECOND)

    assert world.decide(cases.FORMER, False, 1)["warnings"] == [
        f"{cases.FOLLOWUP} belongs to {LEAD}, so {cases.FORMER} cannot decide it"
    ]


def racing_pipelines(world, monkeypatch, times):
    real, raced = world.redis.pipeline, []

    def pipeline():
        pipe = real()
        read = pipe.hget

        def hget(key, field):
            if field == "owners" and len(raced) < times:
                raced.append(field)
                world.redis.hset(key, "seats", json.dumps([LEAD]))
            return read(key, field)

        pipe.hget = hget
        return pipe

    monkeypatch.setattr(world.redis, "pipeline", pipeline)
    return raced


def test_a_retire_racing_another_masters_write_retries_and_still_hands_the_phases_to_the_front_door(world, monkeypatch):
    table = masters.MasterSeats(world.redis)
    [phase] = [p for p, owner in table.owners(SLUG, world.doc()).items() if owner == SECOND]
    raced = racing_pipelines(world, monkeypatch, 1)

    table.retire(SLUG, SECOND)

    assert raced == ["owners"]
    assert json.loads(world.redis.hget(table.key(SLUG), "owners"))[phase] == LEAD
    assert table.retired(SLUG) == [SECOND]


def test_a_retire_that_keeps_losing_the_race_is_refused_naming_the_seat(world, monkeypatch):
    table = masters.MasterSeats(world.redis)
    table.owners(SLUG, world.doc())
    raced = racing_pipelines(world, monkeypatch, masters.SAVE_ATTEMPTS)

    with pytest.raises(masters.MasterError) as refused:
        table.retire(SLUG, SECOND)

    assert str(refused.value) == f"master seats of {SLUG} kept changing; seat {SECOND} was not retired"
    assert len(raced) == masters.SAVE_ATTEMPTS
    assert table.retired(SLUG) == []


def test_a_single_live_master_messages_without_reading_the_ledger(world, monkeypatch):
    world.down(SECOND_NAME)
    monkeypatch.setattr(master_fence, "_ledger_state", lambda slug: pytest.fail("read the ledger"))

    master_fence.check_send(inbox(world), LEAD_NAME, CLAIMANT)


def test_the_switch_off_lets_a_backend_master_message_through(world, monkeypatch):
    monkeypatch.setenv(master_fence.SWITCH, master_fence.OFF)

    master_fence.check_send(inbox(world), SECOND_NAME, CLAIMANT)


def test_an_unreadable_ledger_lets_the_message_through_with_a_fail_open_row(world, monkeypatch, tmp_path):
    def down(slug):
        raise ConnectionError("ledger down")

    rows = []
    monkeypatch.setattr(master_fence, "_ledger_state", down)
    monkeypatch.setattr(master_fence.log, "append", lambda slug, row, home=None: rows.append((slug, row)))

    master_fence.check_send(inbox(world), SECOND_NAME, CLAIMANT)

    assert [(slug, row.gate, row.kind, row.agent, row.tool, row.reason) for slug, row in rows] == [
        (SLUG, "master-fence", "fail-open", SECOND_NAME, "inbox", "ConnectionError: ledger down")
    ]


def test_swarm_say_from_a_backend_master_to_the_claimant_is_refused(world):
    with pytest.raises(InboxError, match=f"so {SECOND} cannot message its claimant"):
        delivery.send(world.store, SLUG, "Cut over now", sender=SECOND_NAME, to=CLAIMANT)

    assert inbox(world).mailbox(ENGINEER) == []


def test_a_queued_swarm_say_from_a_backend_master_is_refused_on_replay(world):
    entry = {"args": {"slug": SLUG, "to": CLAIMANT, "text": "Cut over now", "fyi": False}, "item": "i-say"}

    with pytest.raises(InboxError, match=f"belongs to {LEAD}, so {SECOND} cannot message its claimant"):
        delivery.apply_say(inbox(world), SECOND_NAME, entry)
    assert inbox(world).mailbox(ENGINEER) == []


def test_msg_send_from_a_backend_master_to_the_claimant_is_refused(world):
    record = {"op": "send", "by": SECOND_NAME, "item": "i-send", "args": {"address": CLAIMANT, "text": "Cut over"}}

    with pytest.raises(InboxError, match=f"so {SECOND} cannot message its claimant"):
        inbox_cli.apply(inbox(world), record | {"args": record["args"] | {"fyi": False}})


def test_msg_reply_from_a_backend_master_to_the_claimant_is_refused(world):
    asked = inbox(world).send(CLAIMANT, SECOND, "Replicate without auth?")
    record = {"op": "reply", "by": SECOND_NAME, "item": "i-reply", "args": {"id": asked.id, "text": "Yes, cut over"}}

    with pytest.raises(InboxError, match=f"so {SECOND} cannot message its claimant"):
        inbox_cli.apply(inbox(world), record | {"args": record["args"] | {"fyi": False}})
    assert inbox(world).get(asked.id).state != "done"


def test_the_front_door_replies_to_the_claimant(world):
    asked = inbox(world).send(CLAIMANT, LEAD_NAME, "Replicate without auth?")
    record = {"op": "reply", "by": LEAD_NAME, "item": "i-reply-lead", "args": {"id": asked.id, "text": "Stage it"}}

    assert inbox_cli.apply(inbox(world), record | {"args": record["args"] | {"fyi": False}})["closed"] == asked.id


def test_a_message_check_keeps_the_owner_of_every_phase(world):
    master_fence.check_send(inbox(world), SECOND_NAME, OWN_CLAIMANT)

    saved = json.loads(world.redis.hget(masters.MasterSeats(world.redis).key(SLUG), "owners"))
    assert saved == {"p1": LEAD, "p2": SECOND}


def test_a_message_check_reads_the_ledger_of_the_swarm_it_names(monkeypatch):
    from scripts.swarm import ledger_client

    class Client:
        def state(self, slug):
            return {"slug": slug}

    monkeypatch.setattr(ledger_client, "LedgerClient", Client)

    assert master_fence._ledger_state(SLUG) == {"slug": SLUG}
