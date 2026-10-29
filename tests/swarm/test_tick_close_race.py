import json

import pytest

from scripts.inbox import exits, wake
from scripts.inbox.seen import SeenMarks
from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm import automated_gc, ledger_events, overrun, stall_watch, waits
from scripts.swarm.ledger_events import Mail, PullRequest
from scripts.swarm.store import AgentRecord, RedisStore
from tests.inbox.test_decided_events import crew, queue_event  # noqa: F401
from tests.inbox.test_decided_events import wake as wake_at
from tests.swarm import test_overrun as overrun_case
from tests.swarm import test_stall_watch as stall_case
from tests.swarm.test_automated_gc import LIMIT, START, answer, sweep, world  # noqa: F401
from tests.swarm.test_automated_gc import SLUG as GC_SLUG

pytestmark = pytest.mark.unit

NAME = "engineer@abc-0001"
SEAT = "eng-1@sw"
OTHER_PATH = "cancelled: closed by another path"
NOTICE = "Your wait on checks has ended. Pick task t1 back up: read your checks."


class Race:
    """Once armed, withdraws each item a tick step closes just before that close runs."""

    def __init__(self):
        self.armed, self.raced = False, []

    def arm(self):
        self.armed = True

    def closed_meanwhile(self, inbox, item):
        found = inbox.get(item.id)
        return item.id in self.raced and (found.state, found.reason) == ("cancelled", OTHER_PATH)


@pytest.fixture
def race(monkeypatch):
    found, move = Race(), InboxStore._move

    def racing(self, item_id, by, actors, state, reason, only_from=None):
        if found.armed and state in CLOSED and item_id not in found.raced:
            found.raced.append(item_id)
            assert self.withdraw(item_id, "another-path", OTHER_PATH)
        return move(self, item_id, by, actors, state, reason, only_from)

    monkeypatch.setattr(InboxStore, "_move", racing)
    return found


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def test_settle_closes_an_open_item_and_passes_one_closed_or_gone(store):
    inbox = InboxStore(store.redis)
    item = inbox.send("swarm", "bob", "hi")
    assert inbox.settle(item.id, "swarm", "done", "handled").state == "done"
    assert inbox.settle(item.id, "swarm", "done", "again") is None
    assert inbox.get(item.id).reason == "done: handled"
    assert inbox.settle("gone1", "swarm", "done", "handled") is None


def test_settle_passes_an_item_closed_meanwhile(store, race):
    inbox = InboxStore(store.redis)
    item = inbox.send("swarm", "bob", "hi")
    race.arm()
    assert inbox.settle(item.id, "swarm", "done", "handled") is None
    assert race.closed_meanwhile(inbox, item)


def test_wake_passes_a_decided_event_closed_meanwhile(crew, race):  # noqa: F811
    _, inbox, ledger, herdr = crew
    item = queue_event(crew, "questions", "added")
    ledger.doc["questions"][0].update({"answers": [{"text": "Use port 8765", "by": "operator"}]})
    race.arm()
    wake_at(crew, item.created_at + 1)
    assert race.closed_meanwhile(inbox, item)
    assert herdr.prompts == []


def test_wake_passes_an_item_seen_on_the_ledger_and_closed_meanwhile(crew, race):  # noqa: F811
    store, inbox, ledger, herdr = crew
    item = inbox.send("sw-eng-1", "master@sw", "a comment", ref="sw:3:c1")
    assert SeenMarks(store.redis).mark("sw-master-1", "sw:3:c1")
    race.arm()
    wake.wake_pass(inbox, "sw", store.agents("sw"), herdr, ledger, item.created_at + 1, 300_000)
    assert race.closed_meanwhile(inbox, item)


def test_exits_pass_a_life_notice_closed_meanwhile(store, race):
    inbox = InboxStore(store.redis)
    item = inbox.send(exits.BY, NAME, "push and hand off", ref="sw:overrun:t1")
    race.arm()
    exits.settle(inbox, NAME, "", "left its seat")
    assert race.closed_meanwhile(inbox, item)


def test_exits_pass_taken_seat_mail_closed_meanwhile(store, race):
    inbox = InboxStore(store.redis)
    inbox.seats.occupy(SEAT, NAME, at=1)
    item = inbox.send(exits.BY, SEAT, "push and hand off", ref="sw:overrun:t1")
    inbox.deliver(item.id, NAME)
    race.arm()
    exits.settle(inbox, NAME, "", "left its seat")
    assert race.closed_meanwhile(inbox, item)


def test_exits_pass_a_seat_notice_closed_meanwhile(store, race):
    inbox = InboxStore(store.redis)
    item = inbox.send(waits.SENDER, SEAT, NOTICE)
    race.arm()
    exits._settle_seat_notices(inbox, {SEAT}, set())
    assert race.closed_meanwhile(inbox, item)


def test_waits_pass_a_wait_ended_notice_closed_meanwhile(store, race):
    inbox = InboxStore(store.redis)
    item = inbox.send(waits.SENDER, SEAT, NOTICE)
    race.arm()
    waits.settle_notices(inbox, AgentRecord(NAME, "eng", "t1", seat=SEAT), "progress")
    assert race.closed_meanwhile(inbox, item)


def test_stall_watch_passes_a_superseded_alert_closed_meanwhile(race):
    swarm = stall_case.Swarm(stall_case.GREEN)
    swarm.tick(stall_case.START)
    swarm.tick(stall_case.START + 10 * stall_case.MINUTE)
    [first] = swarm.inbox()
    swarm.ledger.rows["t1"].update(state="pr", claimed_by=stall_case.OWNER)
    race.arm()
    [action] = swarm.tick(stall_case.START + 11 * stall_case.MINUTE)
    assert "for 11 minutes" in action
    assert race.closed_meanwhile(InboxStore(swarm.store.redis), first)


def test_stall_watch_passes_a_moot_alert_closed_meanwhile(race):
    swarm = stall_case.Swarm(stall_case.GREEN)
    inbox = InboxStore(swarm.store.redis)
    alert = inbox.send(stall_watch.SENDER, "master@sw", "the eng lane is full")
    row = {"case": stall_watch.UNCLAIMED, "lane": "eng", "task": "t9", "item": alert.id, "at": 0, "limit": 15}
    key = swarm.store.key(stall_case.SLUG, stall_watch.ALERTS)
    swarm.store.redis.hset(key, "t9", json.dumps(row))
    race.arm()
    swarm.tick(stall_case.START)
    assert race.closed_meanwhile(inbox, alert)
    assert not swarm.store.redis.hexists(key, "t9")


def test_ledger_events_pass_a_red_notice_closed_meanwhile(store, race):
    inbox = InboxStore(store.redis)
    mail = Mail(inbox, store, "sw")
    item = inbox.send(ledger_events.SENDER, SEAT, "your pull request is red")
    url = "https://github.com/o/r/pull/7"
    mail.track_red(item, url)
    race.arm()
    assert ledger_events._settle_red_notices(mail, lambda _: PullRequest("MERGED", 1, 1, False)) == []
    assert race.closed_meanwhile(inbox, item)
    assert not store.redis.hexists(mail.red_index(), item.id)


def test_overrun_passes_its_ask_closed_meanwhile(race):
    swarm = overrun_case.Swarm()
    swarm.tick(overrun_case.PARK)
    [ask] = swarm.agent_inbox()
    race.arm()
    swarm.tick(overrun_case.ENDED)
    assert swarm.retired == [overrun_case.OWNER]
    assert swarm.ledger.rows["t1"]["state"] == "open"
    assert race.closed_meanwhile(InboxStore(swarm.store.redis), ask)
    assert swarm.store.take_restart(overrun_case.SLUG, "t1") == overrun.CAUSE


def test_automated_activity_records_an_answer_closed_meanwhile(world, race):  # noqa: F811
    _, inbox, agent, _, _ = world
    sweep(world, START + LIMIT)
    reply = answer(world, "nothing to do")
    race.arm()
    assert sweep(world, START + LIMIT + 1) == [f"recorded activity answer from {agent.name}"]
    assert race.closed_meanwhile(inbox, reply)
    assert automated_gc.state(world[0], GC_SLUG, agent)["request"] == ""
