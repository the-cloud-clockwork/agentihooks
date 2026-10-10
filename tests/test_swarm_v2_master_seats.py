import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import ledger_events, operator_mail
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger.watch_ledger import line
from scripts.swarm_v2 import masters

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "sw"
LEAD = f"master@{SLUG}"
SECOND = f"master-2@{SLUG}"
PHASES = ["p1", "p2", "p3", "p4"]
DOC = {
    "phases": [{"id": p, "title": f"phase {p}"} for p in PHASES],
    "tasks": [
        {"id": "t1", "phase": "p1", "state": "open"},
        {"id": "t2", "phase": "p2", "state": "open"},
        {"id": "t4", "phase": "p4", "state": "claimed", "claimed_by": "sw-eng-gone"},
    ],
    "_meta": {"rev": 1, "events": []},
}
FIRST = AgentRecord("sw-master-1", MASTER, MASTER, pane_id="pm1", seat=LEAD)
OTHER = AgentRecord("sw-master-2", MASTER, MASTER, pane_id="pm2", seat=SECOND)


@pytest.fixture
def swarm():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 1, 1))
    masters.MasterSeats(store.redis).set_count(SLUG, 2)
    return store, InboxStore(store.redis)


def seated(store, *agents):
    for agent in agents:
        store.put_agent(SLUG, agent)
        store.seats.occupy(agent.seat, agent.name, 1)


def write(rev, target, text="please look"):
    return {
        "rev": rev,
        "at": rev,
        "by": "operator",
        "kind": "comment added",
        "target": target,
        "id": f"c{rev}",
        "text": text,
    }


def relay(swarm, *events):
    store, inbox = swarm
    return operator_mail.relay(inbox, store, SLUG, DOC, list(events), line)


def texts(inbox, address):
    return [item.text for item in inbox.pending_items(address)]


def test_seat_one_is_the_lead_at_the_single_master_address_and_later_seats_are_numbered():
    assert masters.seats(SLUG, 1) == [LEAD]
    assert masters.seats(SLUG, 3) == [LEAD, SECOND, f"master-3@{SLUG}"]


@pytest.mark.parametrize("count", [0, -1])
def test_a_master_count_below_one_is_refused(swarm, count):
    store, _ = swarm
    with pytest.raises(masters.MasterError):
        masters.seats(SLUG, count)
    with pytest.raises(masters.MasterError):
        masters.MasterSeats(store.redis).set_count(SLUG, count)
    assert masters.MasterSeats(store.redis).count(SLUG) == 2


def test_a_swarm_without_a_count_has_one_master_that_owns_every_phase(swarm):
    store, _ = swarm
    seats = masters.MasterSeats(store.redis)
    assert seats.count("fresh") == 1
    assert set(seats.owners("fresh", DOC).values()) == {"master@fresh"}


def test_four_phases_split_two_and_two_between_two_master_seats(swarm):
    store, _ = swarm
    owners = masters.MasterSeats(store.redis).owners(SLUG, DOC)
    assert owners == {"p1": LEAD, "p2": SECOND, "p3": LEAD, "p4": SECOND}


def test_assignment_is_sticky_and_gives_a_new_phase_to_the_seat_owning_fewest():
    previous = {"p1": SECOND, "p2": SECOND, "p3": LEAD}
    owners = masters.assign(["p1", "p2", "p3", "p4", "p5"], [LEAD, SECOND], previous)
    assert owners == {"p1": SECOND, "p2": SECOND, "p3": LEAD, "p4": LEAD, "p5": LEAD}


def test_a_dropped_phase_loses_its_owner_and_frees_the_seat():
    owners = masters.assign(["p1", "p3"], [LEAD, SECOND], {"p1": LEAD, "p2": SECOND, "p3": LEAD})
    assert owners == {"p1": LEAD, "p3": LEAD}


def test_a_removed_seat_hands_its_phases_on_and_every_other_phase_keeps_its_owner(swarm):
    store, _ = swarm
    seats = masters.MasterSeats(store.redis)
    seats.set_count(SLUG, 3)
    before = seats.owners(SLUG, DOC)
    assert before == {"p1": LEAD, "p2": SECOND, "p3": f"master-3@{SLUG}", "p4": LEAD}
    seats.set_count(SLUG, 2)
    after = seats.owners(SLUG, DOC)
    assert {p: s for p, s in after.items() if p != "p3"} == {p: s for p, s in before.items() if p != "p3"}
    assert after["p3"] == SECOND


def test_owners_read_again_after_a_restart_are_the_same(swarm):
    store, _ = swarm
    first = masters.MasterSeats(store.redis).owners(SLUG, DOC)
    reordered = {**DOC, "phases": list(reversed(DOC["phases"]))}
    assert masters.MasterSeats(store.redis).owners(SLUG, reordered) == first


@pytest.mark.parametrize(
    ("target", "owner"),
    [("phases/p2", SECOND), ("phases/p3", LEAD), ("tasks/t2", SECOND), ("tasks/t2/comments/c1", SECOND)],
)
def test_an_item_about_a_phase_or_its_task_belongs_to_the_phase_owner(swarm, target, owner):
    store, _ = swarm
    owners = masters.MasterSeats(store.redis).owners(SLUG, DOC)
    assert masters.owner_of(target, DOC, owners) == owner


@pytest.mark.parametrize("target", ["chat", "notes/n1", "followups/f1", "tasks/missing", "phases/gone"])
def test_an_item_with_no_phase_has_no_owner(swarm, target):
    store, _ = swarm
    owners = masters.MasterSeats(store.redis).owners(SLUG, DOC)
    assert masters.owner_of(target, DOC, owners) == ""


def test_an_operator_comment_on_a_phase_of_the_second_master_reaches_only_that_master(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    relay(swarm, write(5, "phases/p2"))
    assert len(texts(inbox, SECOND)) == 1 and texts(inbox, LEAD) == []


def test_an_operator_comment_on_an_unclaimed_task_reaches_its_phase_owner(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    relay(swarm, write(5, "tasks/t4/comments/c1"))
    assert len(texts(inbox, SECOND)) == 1 and texts(inbox, LEAD) == []


def test_unaddressed_chat_stays_with_the_lead_which_answers_the_operator(swarm):
    store, inbox = swarm
    seated(store, OTHER, FIRST)
    relay(swarm, write(5, "chat", text="where are we"))
    [text] = texts(inbox, LEAD)
    assert text.endswith(operator_mail.MASTER_RULE) and texts(inbox, SECOND) == []


def test_an_owner_seat_with_no_live_master_never_strands_an_item(swarm):
    store, inbox = swarm
    seated(store, FIRST)
    relay(swarm, write(5, "phases/p2"))
    assert len(texts(inbox, LEAD)) == 1 and texts(inbox, SECOND) == []


def test_the_tick_tells_the_phase_owner_when_a_task_in_its_phase_is_blocked(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    blocked = {"rev": 2, "at": 1000, "by": "sw-eng-1", "kind": "task blocked", "target": "tasks/t2"}
    doc = {**DOC, "_meta": {"rev": 2, "events": [blocked]}}
    ledger_events._events(ledger_events.Mail(inbox, store, SLUG, doc), [blocked], {"t2": DOC["tasks"][1]}, set())
    assert len(texts(inbox, SECOND)) == 1 and texts(inbox, LEAD) == []


def test_the_tick_keeps_a_follow_up_with_the_lead(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    added = {"rev": 2, "at": 1000, "by": "sw-eng-1", "kind": "added", "target": "followups/f1", "text": "x"}
    ledger_events._events(ledger_events.Mail(inbox, store, SLUG, DOC), [added], {}, set())
    assert len(texts(inbox, LEAD)) == 1 and texts(inbox, SECOND) == []
