import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import idle, stale_nudge
from scripts.swarm.store import AgentRecord

pytestmark = pytest.mark.xdist_group("fakeredis")

LIMIT = stale_nudge.LIMIT_MS
NAME = "engineer@a1b2c3-0001"
BASE = 1_000 * LIMIT


@pytest.fixture
def inbox():
    import fakeredis

    return InboxStore(fakeredis.FakeRedis(decode_responses=True))


def agent(started_at=BASE):
    return AgentRecord(NAME, "eng", "t1", started_at=started_at)


@pytest.mark.parametrize(
    ("idle_ms", "wait", "provider_error", "expected"),
    [
        (LIMIT, None, "", False),
        (LIMIT + 1, None, "", True),
        (LIMIT + 1, {"until": BASE + 2 * LIMIT, "reason": "checks on the pull request"}, "", False),
        (LIMIT + 1, {"until": BASE + 2 * LIMIT, "reason": "  "}, "", True),
        (LIMIT + 1, {"until": BASE + 2 * LIMIT, "on": {"kind": "checks"}, "reason": ""}, "", False),
        (LIMIT + 1, {"until": BASE + LIMIT + 1, "reason": "checks"}, "", True),
        (0, None, "model at capacity", True),
        (0, {"until": BASE + 2 * LIMIT, "reason": "checks"}, "model at capacity", True),
    ],
)
def test_an_agent_is_stale_past_ten_minutes_without_a_named_wait_or_at_once_on_a_provider_error(
    idle_ms, wait, provider_error, expected
):
    assert stale_nudge.stale(BASE, wait, provider_error, BASE + idle_ms) is expected


def test_last_activity_is_the_later_of_the_heartbeat_and_the_start(inbox):
    assert stale_nudge.last_activity(inbox.redis, "sw", agent()) == BASE
    idle.beat(inbox.redis, "sw", NAME, idle.IDLE, BASE + 5)
    assert stale_nudge.last_activity(inbox.redis, "sw", agent()) == BASE + 5
    assert stale_nudge.last_activity(inbox.redis, "sw", agent(started_at=BASE + 9)) == BASE + 9


def test_the_item_id_is_stable_inside_a_window_and_changes_with_the_window_swarm_and_agent():
    first = stale_nudge.item_id("sw", NAME, BASE)
    assert first == stale_nudge.item_id("sw", NAME, BASE + LIMIT - 1)
    assert len(first) == 12 and int(first, 16) >= 0
    others = {
        stale_nudge.item_id("sw", NAME, BASE + LIMIT),
        stale_nudge.item_id("other", NAME, BASE),
        stale_nudge.item_id("sw", "engineer@a1b2c3-0002", BASE),
    }
    assert first not in others and len(others) == 3


def test_a_stale_agent_gets_one_inbox_item_per_window(inbox):
    now = BASE + LIMIT + 1
    actions = stale_nudge.nudge(inbox, "sw", agent(), "", now)
    item_id = stale_nudge.item_id("sw", NAME, now)
    assert actions == [f"nudged stale {NAME} through its inbox as message {item_id}"]
    item = inbox.get(item_id)
    assert (item.sender, item.address, item.task, item.state) == ("swarm", NAME, "t1", "pending")
    assert item.ref == f"stale-nudge:{item_id}" and stale_nudge.is_nudge(item)
    assert "no activity from you for over ten minutes" in item.text and "task t1" in item.text
    assert "agentihooks swarm sw wait --on" in item.text
    assert stale_nudge.nudge(inbox, "sw", agent(), "", now + LIMIT // 2) == []
    assert [i.id for i in inbox.inbox(NAME)] == [item_id]
    later = BASE + 2 * LIMIT
    assert stale_nudge.nudge(inbox, "sw", agent(), "", later) != []
    assert [i.id for i in inbox.inbox(NAME)] == [item_id, stale_nudge.item_id("sw", NAME, later)]


def test_a_fresh_agent_or_a_named_wait_gets_no_item(inbox):
    assert stale_nudge.nudge(inbox, "sw", agent(), "", BASE + LIMIT) == []
    idle.declare_wait(inbox.redis, "sw", NAME, BASE + 3 * LIMIT, "checks on the pull request", BASE)
    assert stale_nudge.nudge(inbox, "sw", agent(), "", BASE + 2 * LIMIT) == []
    assert inbox.inbox(NAME) == []


def test_a_provider_error_is_nudged_at_once_and_named_in_the_text(inbox):
    now = BASE + 1
    assert stale_nudge.nudge(inbox, "sw", agent(), "model at capacity", now) != []
    item = inbox.get(stale_nudge.item_id("sw", NAME, now))
    assert "your pane shows a provider error" in item.text


@pytest.mark.parametrize(
    ("sender", "ref", "expected"),
    [
        ("swarm", f"stale-nudge:sw:{NAME}", True),
        ("alice", f"stale-nudge:sw:{NAME}", False),
        ("swarm", "inbox-escalation:abc", False),
        ("swarm", "stale-nudge", False),
    ],
)
def test_only_a_swarm_stale_nudge_counts_as_one(inbox, sender, ref, expected):
    assert stale_nudge.is_nudge(inbox.send(sender, NAME, "text", ref=ref)) is expected
