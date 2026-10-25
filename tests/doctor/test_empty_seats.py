import pytest

from scripts.doctor import inbox, read
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig


@pytest.mark.parametrize("slug", ["demo", "demo@partition"])
def test_unread_or_taken_items_on_an_empty_successor_seat_are_not_past_window_until_a_live_agent_binds(slug):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(slug, "/r", max_eng=1, max_ci=2))
    box = InboxStore(store.redis)
    row = {
        "id": "waiting",
        "sender": "swarm",
        "address": f"ci-2@{slug}",
        "text": "Coordinate shared work",
        "state": "pending",
        "created_at": 60_000,
        "history": [],
    }
    receivers = read.inbox_receivers(store, box, slug, [row])
    assert receivers[row["address"]] == inbox.Receiver(seat_wait=True)
    assert inbox.past_window([row], 660_000, 300_000, receivers) == []
    delivered = {**row, "id": "taken", "state": "delivered", "history": [{"state": "delivered", "by": "demo-ci-1"}]}
    receivers = read.inbox_receivers(store, box, slug, [delivered])
    assert inbox.past_window([delivered], 660_000, 300_000, receivers) == []

    store.seats.occupy(row["address"], "demo-ci-2", 1)
    store.put_agent(slug, AgentRecord(name="demo-ci-2", lane="ci", task="t2", harness="codex"))
    receivers = read.inbox_receivers(store, box, slug, [row])
    [finding] = inbox.past_window([row], 660_000, 300_000, receivers)
    assert finding.summary == "unread delivery; sent 10 minutes ago"
    assert any("occupant demo-ci-2; harness codex" in evidence for evidence in finding.evidence)


def test_a_departed_occupant_is_empty_even_when_its_agent_record_still_says_working():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/r", 1, 1))
    box = InboxStore(store.redis)
    store.seats.occupy("ci-1@demo", "demo-ci-1", 1)
    store.put_agent("demo", AgentRecord(name="demo-ci-1", lane="ci", task="t1", state="working", harness="claude"))
    store.seats.record_exit("demo-ci-1", "ci-1@demo", "left")
    item = {"address": "ci-1@demo", "history": []}
    receiver = read.inbox_receivers(store, box, "demo", [item])[item["address"]]
    assert receiver == inbox.Receiver(seat_wait=True, bound_at=1)


@pytest.mark.parametrize(
    "state,cap,address",
    [
        ("stopped", 1, "ci-1@demo"),
        ("running", 0, "ci-1@demo"),
        ("running", 1, "ci-2@demo"),
        ("stopped", 1, "master@demo"),
        ("stopped", 1, "ci-1@demo@partition"),
    ],
)
def test_a_seat_with_no_possible_successor_keeps_unread_delivery(state, cap, address):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    slug = address.partition("@")[2]
    store.create(SwarmConfig(slug, "/r", 1, cap, state=state))
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
    receivers = read.inbox_receivers(store, box, slug, [row])
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
    assert all("work" in finding.evidence for finding in found)


@pytest.mark.parametrize(
    "address,receivers,expected",
    [
        ("ci-1@demo", None, "recipient state unavailable"),
        (
            "ci-1@demo",
            {"ci-1@demo": inbox.Receiver(live=True, occupant="demo-ci-1")},
            "occupant demo-ci-1; harness unknown",
        ),
        ("demo-ci-1", {"demo-ci-1": inbox.Receiver()}, "recipient has no live occupant"),
    ],
)
def test_inbox_findings_report_missing_receiver_harness_and_departed_personal_recipient(address, receivers, expected):
    row = {
        "id": "waiting",
        "sender": "swarm",
        "address": address,
        "text": "work",
        "state": "pending",
        "created_at": 0,
        "history": [],
    }
    [finding] = inbox.findings([row], 600_000, 300_000, receivers)
    assert expected in finding.evidence
