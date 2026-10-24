import string

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import idle, stale_nudge
from scripts.swarm.store import AgentRecord

LIMIT = stale_nudge.LIMIT_MS
NAME = "engineer@a1b2c3-0001"
BASE = 1_000 * LIMIT
WAIT = {"until": BASE + 3 * LIMIT, "reason": "checks on the pull request"}
IDLE_BEAT = {"state": idle.IDLE, "at": BASE}
WORKING_BEAT = {"state": idle.WORKING, "at": BASE}


@pytest.fixture
def inbox():
    import fakeredis

    return InboxStore(fakeredis.FakeRedis(decode_responses=True))


def agent(started_at=BASE):
    return AgentRecord(NAME, "eng", "t1", started_at=started_at)


@pytest.mark.parametrize(
    ("beat", "idle_ms", "wait", "provider_error", "expected"),
    [
        (IDLE_BEAT, LIMIT, None, "", False),
        (IDLE_BEAT, LIMIT + 1, None, "", True),
        (WORKING_BEAT, LIMIT + 1, None, "", True),
        (None, LIMIT + 1, None, "", True),
        (IDLE_BEAT, LIMIT + 1, WAIT, "", False),
        (IDLE_BEAT, LIMIT + 1, {**WAIT, "reason": "  "}, "", True),
        (IDLE_BEAT, LIMIT + 1, {"until": WAIT["until"], "on": {"kind": "checks"}, "reason": ""}, "", False),
        (IDLE_BEAT, LIMIT + 1, {**WAIT, "until": BASE + LIMIT + 1}, "", True),
        (IDLE_BEAT, 0, None, "model at capacity", True),
        (None, 0, None, "model at capacity", True),
        (IDLE_BEAT, 0, WAIT, "model at capacity", True),
        (WORKING_BEAT, 0, None, "model at capacity", False),
        (WORKING_BEAT, LIMIT, None, "model at capacity", False),
        (WORKING_BEAT, LIMIT + 1, WAIT, "model at capacity", True),
    ],
)
def test_stale_past_ten_silent_minutes_without_a_named_wait_or_on_a_provider_error_outside_a_working_turn(
    beat, idle_ms, wait, provider_error, expected
):
    assert stale_nudge.stale(beat, BASE, wait, provider_error, BASE + idle_ms) is expected


@pytest.mark.parametrize(("provider_error", "expected"), [("", False), ("model at capacity", True)])
def test_an_agent_with_no_known_activity_time_is_stale_only_on_a_provider_error(provider_error, expected):
    assert stale_nudge.stale(None, 0, None, provider_error, BASE) is expected


def test_a_wait_without_an_expiry_does_not_hide_a_stalled_agent():
    assert stale_nudge.stale(IDLE_BEAT, BASE, {"reason": "checks"}, "", BASE + LIMIT + 1)


@pytest.mark.parametrize(
    ("sender", "ref", "age_ms", "expected"),
    [
        ("swarm", "stale-nudge:abc", 0, True),
        ("swarm", "stale-nudge:abc", stale_nudge.FRESH_MS, True),
        ("swarm", "stale-nudge:abc", stale_nudge.FRESH_MS + 1, False),
        ("alice", "stale-nudge:abc", 0, False),
    ],
)
def test_only_a_fresh_swarm_nudge_interrupts(inbox, sender, ref, age_ms, expected):
    item = inbox.send(sender, NAME, "text", ref=ref)
    assert stale_nudge.interrupts(item, item.created_at + age_ms) is expected


@pytest.mark.parametrize(
    ("beat", "started_at", "expected"),
    [(None, BASE, BASE), ({"at": BASE + 5}, BASE, BASE + 5), ({"at": BASE + 5}, BASE + 9, BASE + 9), ({}, 0, 0)],
)
def test_last_activity_is_the_later_of_the_heartbeat_and_the_start(beat, started_at, expected):
    assert stale_nudge.last_activity(beat, agent(started_at)) == expected


def test_the_window_counts_whole_ten_minute_spans_of_silence():
    assert [stale_nudge.window(BASE, BASE + at) for at in (0, LIMIT - 1, LIMIT, 2 * LIMIT + 5)] == [0, 0, 1, 2]


def test_the_item_id_is_stable_inside_a_window_of_silence_and_changes_with_each_input():
    first = stale_nudge.item_id("sw", NAME, BASE, BASE + LIMIT)
    assert first == stale_nudge.item_id("sw", NAME, BASE, BASE + 2 * LIMIT - 1)
    assert len(first) == 12 and set(first) <= set(string.hexdigits.lower())
    others = {
        stale_nudge.item_id("sw", NAME, BASE, BASE + 2 * LIMIT),
        stale_nudge.item_id("sw", NAME, BASE + 1, BASE + LIMIT),
        stale_nudge.item_id("other", NAME, BASE, BASE + LIMIT),
        stale_nudge.item_id("sw", "engineer@a1b2c3-0002", BASE, BASE + LIMIT),
    }
    assert first not in others and len(others) == 4


def nudges(inbox):
    return inbox.inbox(NAME)


def test_a_stale_agent_gets_one_inbox_item_per_window_of_silence(inbox):
    idle.beat(inbox.redis, "sw", NAME, idle.IDLE, BASE)
    now = BASE + LIMIT + 1
    actions = stale_nudge.nudge(inbox, "sw", agent(), "", now)
    item_id = stale_nudge.item_id("sw", NAME, BASE, now)
    assert actions == [f"nudged stale {NAME} through its inbox as message {item_id}"]
    item = inbox.get(item_id)
    assert (item.sender, item.address, item.task, item.state) == ("swarm", NAME, "t1", "pending")
    assert item.ref == f"stale-nudge:{item_id}" and stale_nudge.is_nudge(item)
    assert "no activity from you for over ten minutes" in item.text and "task t1" in item.text
    assert "agentihooks swarm sw wait --on" in item.text
    assert stale_nudge.nudge(inbox, "sw", agent(), "", BASE + 2 * LIMIT - 1) == []
    assert [i.id for i in nudges(inbox)] == [item_id]
    assert stale_nudge.nudge(inbox, "sw", agent(), "", BASE + 2 * LIMIT) != []
    assert len(nudges(inbox)) == 2


def test_new_activity_starts_a_new_series(inbox):
    first = BASE + LIMIT + 1
    assert stale_nudge.nudge(inbox, "sw", agent(), "", first) != []
    idle.beat(inbox.redis, "sw", NAME, idle.IDLE, first + 5)
    assert stale_nudge.nudge(inbox, "sw", agent(), "", first + 10) == []
    assert stale_nudge.nudge(inbox, "sw", agent(), "", first + 6 + LIMIT) != []
    assert len(nudges(inbox)) == 2


def test_a_fresh_agent_or_a_named_wait_gets_no_item(inbox):
    assert stale_nudge.nudge(inbox, "sw", agent(), "", BASE + LIMIT) == []
    idle.declare_wait(inbox.redis, "sw", NAME, BASE + 3 * LIMIT, "checks on the pull request", BASE)
    assert stale_nudge.nudge(inbox, "sw", agent(), "", BASE + 2 * LIMIT) == []
    assert nudges(inbox) == []


def test_a_provider_error_is_nudged_at_once_and_named_in_the_text(inbox):
    now = BASE + 1
    assert stale_nudge.nudge(inbox, "sw", agent(), "model at capacity", now) != []
    item = inbox.get(stale_nudge.item_id("sw", NAME, BASE, now))
    assert "your pane shows a provider error" in item.text
    assert stale_nudge.nudge(inbox, "sw", agent(), "model at capacity", now + 60_000) == []


def test_a_provider_error_under_a_working_heartbeat_waits_for_the_silence_limit(inbox):
    idle.beat(inbox.redis, "sw", NAME, idle.WORKING, BASE)
    assert stale_nudge.nudge(inbox, "sw", agent(), "model at capacity", BASE + LIMIT) == []
    assert stale_nudge.nudge(inbox, "sw", agent(), "model at capacity", BASE + LIMIT + 1) != []


@pytest.mark.parametrize(
    ("sender", "ref", "expected"),
    [
        ("swarm", "stale-nudge:abc", True),
        ("alice", "stale-nudge:abc", False),
        ("swarm", "stale-restart:abc", False),
        ("swarm", "inbox-escalation:abc", False),
        ("swarm", "stale-nudge", False),
    ],
)
def test_only_a_swarm_stale_item_counts_as_a_nudge(inbox, sender, ref, expected):
    assert stale_nudge.is_nudge(inbox.send(sender, NAME, "text", ref=ref)) is expected
