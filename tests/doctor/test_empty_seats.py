import fakeredis
import pytest

from scripts.doctor import inbox, read
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig


def test_empty_successor_seat_is_an_aged_wait_until_a_live_agent_binds():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/r", max_eng=1, max_ci=2))
    box = InboxStore(store.redis)
    row = {
        "id": "waiting",
        "sender": "swarm",
        "address": "ci-2@demo",
        "text": "Coordinate shared work",
        "state": "pending",
        "created_at": 60_000,
        "history": [],
    }
    receivers = read.inbox_receivers(store, box, "demo", [row])
    [finding] = inbox.past_window([row], 660_000, 300_000, receivers)
    assert finding.summary == "seat wait; sent 10 minutes ago"
    assert finding.measure == 10
    assert any("seat is empty" in evidence for evidence in finding.evidence)

    store.seats.occupy(row["address"], "demo-ci-2", 1)
    store.put_agent("demo", AgentRecord(name="demo-ci-2", lane="ci", task="t2", harness="codex"))
    receivers = read.inbox_receivers(store, box, "demo", [row])
    [finding] = inbox.past_window([row], 660_000, 300_000, receivers)
    assert finding.summary == "unread delivery; sent 10 minutes ago"
    assert any("occupant demo-ci-2; harness codex" in evidence for evidence in finding.evidence)


def test_a_departed_occupant_is_empty_even_when_its_agent_record_still_says_working():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/r", 1, 1))
    box = InboxStore(store.redis)
    store.seats.occupy("ci-1@demo", "demo-ci-1", 1)
    store.put_agent("demo", AgentRecord(name="demo-ci-1", lane="ci", task="t1", state="working", harness="claude"))
    store.seats.record_exit("demo-ci-1", "ci-1@demo", "left")
    item = {"address": "ci-1@demo", "history": []}
    receiver = read.inbox_receivers(store, box, "demo", [item])[item["address"]]
    assert receiver == inbox.Receiver(seat_wait=True)


@pytest.mark.parametrize(
    "state,cap,address", [("stopped", 1, "ci-1@demo"), ("running", 0, "ci-1@demo"), ("running", 1, "ci-2@demo")]
)
def test_a_seat_with_no_possible_successor_keeps_unread_delivery(state, cap, address):
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/r", 1, cap, state=state))
    box = InboxStore(store.redis)
    row = {
        "id": "closed-swarm",
        "sender": "swarm",
        "address": address,
        "text": "work",
        "state": "pending",
        "created_at": 0,
        "history": [],
    }
    receivers = read.inbox_receivers(store, box, "demo", [row])
    [finding] = inbox.past_window([row], 600_000, 300_000, receivers)
    assert finding.summary == "unread delivery; sent 10 minutes ago"
    assert "seat is empty" in finding.evidence


def test_every_inbox_finding_names_the_current_seat_occupant_and_harness():
    pending = {
        "id": "pending",
        "sender": "swarm",
        "address": "ci-1@demo",
        "text": "work",
        "state": "pending",
        "created_at": 0,
        "history": [{"event": "escalated_master", "reason": "nobody read"}],
    }
    closed = {
        **pending,
        "id": "closed",
        "state": "done",
        "reason": "",
        "history": [{"state": "done", "by": "demo-ci-1"}],
    }
    receiver = {"ci-1@demo": inbox.Receiver(live=True, occupant="demo-ci-1", harness="claude")}
    found = inbox.findings([pending, closed], 600_000, 300_000, receiver)
    assert {finding.kind for finding in found} == {"inbox past window", "inbox escalation", "inbox no outcome"}
    assert all("occupant demo-ci-1; harness claude" in finding.evidence for finding in found)
