import pytest

from scripts.doctor import inbox, read
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.mark.parametrize(
    "address,notice_task,held_task,category",
    [
        ("ci-1@demo", "t1", "t2", "task ownership mismatch"),
        ("ci-1@demo", "t1", "t1", "unread delivery"),
        ("ci-1@demo", "", "t2", "unread delivery"),
        ("demo-ci-2", "t1", "t2", "unread delivery"),
    ],
)
def test_detector_names_pending_task_mail_for_different_seat_work(address, notice_task, held_task, category):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/r", 1, 1))
    box = InboxStore(store.redis)
    store.seats.occupy("ci-1@demo", "demo-ci-2", 1)
    store.put_agent("demo", AgentRecord(name="demo-ci-2", lane="ci", task=held_task, seat="ci-1@demo"))
    row = {
        "id": "old-task",
        "sender": "swarm",
        "address": address,
        "text": "Finish the previous task",
        "state": "pending",
        "created_at": 60_000,
        "task": notice_task,
        "history": [{"state": "delivered", "by": "demo-ci-1", "at": 60_001}],
    }
    receivers = read.inbox_receivers(store, box, "demo", [row])
    [finding] = inbox.past_window([row], 660_000, 300_000, receivers)
    assert finding.summary == f"{category}; sent 10 minutes ago"
    assert finding.id == "inbox-past-window/old-task"
    assert finding.measure == 10
    if category == "task ownership mismatch":
        assert "notice task t1; occupant holds t2" in finding.evidence
        later = inbox.past_window([row], 720_000, 300_000, receivers)[0]
        assert later.evidence_identity == finding.evidence_identity
        store.put_agent("demo", AgentRecord(name="demo-ci-2", lane="ci", task="t3", seat="ci-1@demo"))
        changed = read.inbox_receivers(store, box, "demo", [row])
        assert inbox.past_window([row], 720_000, 300_000, changed)[0].evidence_identity != finding.evidence_identity
