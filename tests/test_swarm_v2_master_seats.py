import json

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, ledger_events, operator_mail
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
    owners = masters.assign(["p1", "p2", "p3", "p4", "p5"], [LEAD, SECOND], previous, [LEAD, SECOND])
    assert owners == {"p1": SECOND, "p2": SECOND, "p3": LEAD, "p4": LEAD, "p5": LEAD}


def test_an_uneven_split_stays_put_when_a_phase_is_added_and_no_seat_is_new():
    previous = {"p1": LEAD, "p2": LEAD, "p3": LEAD, "p4": SECOND}
    owners = masters.assign(["p1", "p2", "p3", "p4", "p5"], [LEAD, SECOND], previous, [LEAD, SECOND])
    assert owners == {**previous, "p5": SECOND}


def test_a_dropped_phase_loses_its_owner_and_frees_the_seat():
    previous = {"p1": LEAD, "p2": SECOND, "p3": LEAD}
    owners = masters.assign(["p1", "p3"], [LEAD, SECOND], previous, [LEAD, SECOND])
    assert owners == {"p1": LEAD, "p3": LEAD}


def test_the_lowest_numbered_live_master_answers_while_the_lead_seat_is_empty():
    assert masters.lead_of(SLUG, [f"master-3@{SLUG}", SECOND]) == SECOND
    assert masters.lead_of(SLUG, [SECOND, LEAD]) == LEAD
    assert masters.lead_of(SLUG, []) == LEAD
    assert masters.lead_of(SLUG, ["sw-master-9", f"master-3@{SLUG}"]) == f"master-3@{SLUG}"


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


def test_a_new_reader_after_a_restart_gets_the_saved_owners_whatever_the_phase_order(swarm):
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


def test_an_operator_comment_on_a_task_whose_claimant_is_gone_reaches_its_phase_owner(swarm):
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


class Ledger:
    def __init__(self):
        self.raised = []

    def priority(self, slug, item, text):
        self.raised.append(item)


def tick(store, inbox, doc, now_ms=1000, github=lambda url: None):
    return ledger_events.event_pass(inbox, store, SLUG, doc, Ledger(), now_ms, github)


def with_events(*events, **extra):
    return {**DOC, **extra, "_meta": {"rev": 2, "events": list(events)}}


def test_the_tick_tells_the_phase_owner_when_a_task_in_its_phase_is_blocked(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    tick(store, inbox, DOC)
    tick(
        store,
        inbox,
        with_events({"rev": 2, "at": 1000, "by": "sw-eng-1", "kind": "task blocked", "target": "tasks/t2"}),
    )
    assert len(texts(inbox, SECOND)) == 1 and texts(inbox, LEAD) == []


def test_the_tick_keeps_a_follow_up_with_the_lead(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    tick(store, inbox, DOC)
    added = {"rev": 2, "at": 1000, "by": "sw-eng-1", "kind": "added", "target": "followups/f1", "text": "x"}
    tick(store, inbox, with_events(added, followups=[{"id": "f1", "text": "x", "done": False}]))
    assert len(texts(inbox, LEAD)) == 1 and texts(inbox, SECOND) == []


def test_a_merged_pull_request_left_open_reaches_its_phase_owner_when_its_engineer_is_gone(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    url = "https://example.test/pull/1"
    task = {"id": "t2", "phase": "p2", "state": "pr", "pr_url": url, "claimed_by": "sw-eng-gone"}
    doc = {**DOC, "tasks": [task]}
    merged = ledger_events.PullRequest("MERGED", 1, 1, False)
    tick(store, inbox, doc, now_ms=1 + 21 * ledger_events.MINUTE_MS, github=lambda _: merged)
    assert len(texts(inbox, SECOND)) == 2 and texts(inbox, LEAD) == []


def test_a_new_priority_on_a_task_reaches_its_phase_owner(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    tick(store, inbox, DOC)
    tick(store, inbox, {**DOC, "priorities": [{"item": "tasks/t2", "text": "decide", "by": "sw-eng-1"}]})
    assert len(texts(inbox, SECOND)) == 1 and texts(inbox, LEAD) == []


def test_a_health_finding_about_a_task_reaches_its_phase_owner_and_others_reach_the_lead(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    shown = [
        {"id": "stale-claim/t2", "kind": "stale claim", "subject": "t2", "summary": "quiet"},
        {"id": "ceremony/sw-eng-1", "kind": "ceremony", "subject": "sw-eng-1", "summary": "busy"},
    ]
    ledger_events.findings_pass(inbox, store, SLUG, shown, DOC)
    assert ["stale claim" in t for t in texts(inbox, SECOND)] == [True]
    assert ["ceremony" in t for t in texts(inbox, LEAD)] == [True]


def test_raising_the_master_count_moves_phases_to_the_new_seat(swarm):
    store, _ = swarm
    seats = masters.MasterSeats(store.redis)
    seats.set_count(SLUG, 1)
    assert set(seats.owners(SLUG, DOC).values()) == {LEAD}
    seats.set_count(SLUG, 2)
    assert seats.owners(SLUG, DOC) == {"p1": LEAD, "p2": LEAD, "p3": SECOND, "p4": SECOND}


def test_an_operator_line_to_at_master_reaches_only_the_lead(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    relay(swarm, write(5, "chat", text="@master where are we"))
    assert len(texts(inbox, LEAD)) == 1 and texts(inbox, SECOND) == []


def test_with_the_lead_seat_empty_the_lowest_numbered_live_master_answers_the_operator(swarm):
    store, inbox = swarm
    third = AgentRecord("sw-master-3", MASTER, MASTER, pane_id="pm3", seat=f"master-3@{SLUG}")
    seated(store, third, OTHER)
    relay(swarm, write(5, "chat", text="where are we"), write(6, "phases/p1"))
    assert len(texts(inbox, SECOND)) == 2 and texts(inbox, LEAD) == [] and texts(inbox, third.seat) == []


def test_a_single_master_swarm_routes_a_phase_item_to_its_one_master_as_before(swarm):
    store, inbox = swarm
    masters.MasterSeats(store.redis).set_count(SLUG, 1)
    lone = AgentRecord("sw-master-9", MASTER, MASTER, pane_id="pm9", seat="")
    store.put_agent(SLUG, lone)
    relay(swarm, write(5, "phases/p2"))
    assert len(texts(inbox, lone.name)) == 1 and texts(inbox, LEAD) == []


def test_the_master_count_text_reads_a_whole_number():
    assert masters.count_of("2") == 2


@pytest.mark.parametrize("text", ["0", "two", "-1", "", "²"])
def test_the_master_count_text_must_be_a_whole_number_of_at_least_one(text):
    with pytest.raises(masters.MasterError):
        masters.count_of(text)


def test_swarm_set_masters_stores_the_count_and_reports_it(swarm, monkeypatch, capsys):
    store, _ = swarm
    store.create(SwarmConfig("paused-sw", "/repo", 1, 1, state="paused"))
    monkeypatch.setattr(cli, "connect", lambda: store)
    assert cli.main(["paused-sw", "set", "masters=3"]) == 0
    assert masters.MasterSeats(store.redis).count("paused-sw") == 3
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["masters"] == 3
    assert cli.main(["paused-sw", "set", "masters=0"]) != 0
    assert cli.main(["paused-sw", "set", "masters=4", "bogus=1"]) != 0
    assert masters.MasterSeats(store.redis).count("paused-sw") == 3


def test_swarm_set_refuses_a_master_count_holding_another_equals_sign(swarm, monkeypatch, capsys):
    store, _ = swarm
    store.create(SwarmConfig("paused-sw", "/repo", 1, 1, state="paused"))
    monkeypatch.setattr(cli, "connect", lambda: store)
    assert cli.main(["paused-sw", "set", "masters=2=3"]) == 1
    assert capsys.readouterr().err.splitlines()[-1] == "swarm: masters takes a whole number of master seats, not '2=3'"
    assert masters.MasterSeats(store.redis).count("paused-sw") == 1


def test_refusals_name_the_count_they_refused():
    with pytest.raises(masters.MasterError) as below:
        masters.seats(SLUG, 0)
    assert str(below.value) == "a swarm needs at least one master seat, not 0"
    with pytest.raises(masters.MasterError) as text:
        masters.count_of("two")
    assert str(text.value) == "masters takes a whole number of master seats, not 'two'"


def test_the_lowest_numbered_live_master_is_read_as_a_number_not_as_text():
    assert masters.lead_of(SLUG, [f"master-10@{SLUG}", f"master-2@{SLUG}"]) == f"master-2@{SLUG}"


def test_an_added_seat_takes_the_last_phases_of_the_busiest_seat():
    third = f"master-3@{SLUG}"
    previous = {"p1": LEAD, "p2": SECOND, "p3": SECOND, "p4": SECOND, "p5": SECOND, "p6": LEAD}
    owners = masters.assign(["p1", "p2", "p3", "p4", "p5", "p6"], [LEAD, SECOND, third], previous, [LEAD, SECOND])
    assert owners == {"p1": LEAD, "p2": SECOND, "p3": SECOND, "p4": third, "p5": third, "p6": LEAD}


def test_an_added_seat_takes_from_the_lower_numbered_seat_when_two_are_equally_busy():
    third = f"master-3@{SLUG}"
    previous = {"p1": LEAD, "p2": LEAD, "p3": SECOND, "p4": SECOND}
    owners = masters.assign(PHASES, [LEAD, SECOND, third], previous, [LEAD, SECOND])
    assert owners == {"p1": LEAD, "p2": third, "p3": SECOND, "p4": SECOND}


def test_an_item_under_a_phase_belongs_to_the_phase_owner_and_a_ledger_without_tasks_has_no_task_owner():
    owners = {"p2": SECOND}
    assert masters.owner_of("phases/p2/comments/c1", DOC, owners) == SECOND
    assert masters.owner_of("tasks/t2/comments/c1", DOC, owners) == SECOND
    assert masters.owner_of("tasks/t2", {}, owners) == ""


def test_a_ledger_without_phases_has_no_owners(swarm):
    store, _ = swarm
    assert masters.MasterSeats(store.redis).owners(SLUG, {"tasks": []}) == {}


def test_owners_and_seats_are_saved_sorted_beside_the_count(swarm):
    store, _ = swarm
    seats = masters.MasterSeats(store.redis)
    reordered = {**DOC, "phases": list(reversed(DOC["phases"]))}
    assert seats.owners(SLUG, reordered) == {"p4": LEAD, "p3": SECOND, "p2": LEAD, "p1": SECOND}
    assert store.redis.hgetall(seats.key(SLUG)) == {
        "count": "2",
        "owners": json.dumps({"p1": SECOND, "p2": LEAD, "p3": SECOND, "p4": LEAD}, sort_keys=True),
        "seats": json.dumps([LEAD, SECOND]),
    }


def test_a_saved_uneven_split_with_no_new_seat_is_read_back_unchanged(swarm):
    store, _ = swarm
    seats = masters.MasterSeats(store.redis)
    saved = {"p1": LEAD, "p2": LEAD, "p3": LEAD, "p4": SECOND}
    store.redis.hset(seats.key(SLUG), mapping={"owners": json.dumps(saved), "seats": json.dumps([LEAD, SECOND])})
    assert seats.owners(SLUG, DOC) == saved


class Clashing:
    def __init__(self, redis, clashes):
        self.redis, self.clashes = redis, clashes

    def pipeline(self):
        from redis.exceptions import WatchError

        pipe = self.redis.pipeline()
        if self.clashes:
            self.clashes -= 1

            def clash():
                raise WatchError("changed")

            pipe.execute = clash
        return pipe


def test_a_clashing_save_is_tried_again(swarm):
    store, _ = swarm
    owners = masters.MasterSeats(Clashing(store.redis, 1)).owners(SLUG, DOC)
    assert owners == {"p1": LEAD, "p2": SECOND, "p3": LEAD, "p4": SECOND}
    assert json.loads(store.redis.hget(masters.MasterSeats(store.redis).key(SLUG), "owners")) == owners


def test_a_save_that_keeps_clashing_is_refused_by_name(swarm):
    store, _ = swarm
    with pytest.raises(masters.MasterError) as refused:
        masters.MasterSeats(Clashing(store.redis, masters.SAVE_ATTEMPTS)).owners(SLUG, DOC)
    assert str(refused.value) == f"master seats of {SLUG} kept changing; phase owners were not saved"


def test_every_health_finding_sent_is_reported(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    shown = [
        {"id": "stale-claim/t2", "kind": "stale claim", "subject": "t2", "summary": "quiet"},
        {"id": "ceremony/sw-eng-1", "kind": "ceremony", "subject": "sw-eng-1", "summary": "busy"},
    ]
    sent = ledger_events.findings_pass(inbox, store, SLUG, shown, DOC)
    assert [s.split(": ")[0] for s in sent] == [f"told {SECOND}", f"told {LEAD}"]


def test_a_merged_pull_request_left_open_reports_both_notices(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    url = "https://example.test/pull/1"
    doc = {**DOC, "tasks": [{"id": "t2", "phase": "p2", "state": "pr", "pr_url": url, "claimed_by": "sw-eng-gone"}]}
    merged = ledger_events.PullRequest("MERGED", 1, 1, False)
    sent = tick(store, inbox, doc, now_ms=1 + 21 * ledger_events.MINUTE_MS, github=lambda _: merged)
    assert [s for s in sent if ":merged:" in s] == [
        f"told {SECOND}: {url}:merged:engineer",
        f"told {SECOND}: {url}:merged:master",
    ]


def test_a_master_seat_owns_a_hive_and_receives_its_items(swarm):
    store, inbox = swarm
    seated(store, FIRST, OTHER)
    masters.MasterSeats(store.redis).own_hive(SLUG, "anton-1", SECOND)
    assert masters.MasterSeats(store.redis).hive_owners(SLUG) == {"hives/anton-1": SECOND}
    relay(swarm, write(5, "hives/anton-1"))
    assert texts(inbox, SECOND) and not texts(inbox, LEAD)


def test_a_hive_item_goes_to_the_lead_while_its_owner_is_not_live(swarm):
    store, inbox = swarm
    seated(store, FIRST)
    masters.MasterSeats(store.redis).own_hive(SLUG, "anton-1", SECOND)
    relay(swarm, write(6, "hives/anton-1"))
    assert texts(inbox, LEAD) and not texts(inbox, SECOND)


def test_a_seat_outside_the_master_count_cannot_own_a_hive(swarm):
    store, _ = swarm
    seats = masters.MasterSeats(store.redis)
    with pytest.raises(masters.MasterError, match="is not a master seat"):
        seats.own_hive(SLUG, "anton-1", f"master-3@{SLUG}")
    assert seats.hive_owners(SLUG) == {}


def test_a_hive_owned_by_a_removed_seat_returns_to_the_lead(swarm):
    store, _ = swarm
    seats = masters.MasterSeats(store.redis)
    seats.own_hive(SLUG, "anton-1", SECOND)
    seats.set_count(SLUG, 1)
    assert seats.hive_owners(SLUG) == {}
    assert masters.route("hives/anton-1", DOC, lambda: seats.owners(SLUG, DOC), [LEAD], LEAD) == LEAD


def test_a_hive_owner_routes_its_hive_and_leaves_phase_owners_untouched(swarm):
    store, _ = swarm
    seats = masters.MasterSeats(store.redis)
    before = seats.owners(SLUG, DOC)
    seats.own_hive(SLUG, "anton-1", SECOND)
    after = seats.owners(SLUG, DOC)
    assert after == {**before, "hives/anton-1": SECOND}
    assert masters.route("hives/anton-1", DOC, lambda: after, [LEAD, SECOND], LEAD) == SECOND
    assert masters.owner_of("hives/anton-1/x", DOC, after) == SECOND
    assert json.loads(store.redis.hget(seats.key(SLUG), "owners")) == before
