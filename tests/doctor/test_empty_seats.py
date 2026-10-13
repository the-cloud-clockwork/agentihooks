import fakeredis

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
