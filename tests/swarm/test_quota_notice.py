import json
from dataclasses import replace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import quota_notice
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

HURRY = "Your account has fifteen percent or less quota left. Finish the current step and push."


@pytest.fixture
def store():
    import fakeredis

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, code="a1b2c3"))
    return result


def agent(store, **fields):
    row = AgentRecord("engineer@a1b2c3-0001", "eng", "t1", harness="claude", account="a", started_at=100)
    row = replace(row, **fields)
    store.put_agent("sw", row)
    return row


def decision(five=90, week=90, **fields):
    return {
        "accounts": [
            {
                "harness": "claude",
                "name": "a",
                "state": "NORMAL",
                "sessions": 1,
                "five_left": five,
                "week_left": week,
                **fields,
            }
        ]
    }


def messages(store, row):
    return InboxStore(store.redis).inbox(row.name)


@pytest.mark.parametrize("five,week", [(15, 90), (90, 15), (14.9, 90), (90, 14.9)])
def test_hurry_at_either_window_threshold(store, five, week):
    row = agent(store)
    actions = quota_notice.apply("sw", store, decision(five, week))
    assert actions == [f"sent {row.name} the quota hurry"]
    items = messages(store, row)
    assert len(items) == 1
    assert items[0].text == HURRY
    assert items[0].sender == "swarm"
    assert items[0].address == row.name
    assert not items[0].fyi


@pytest.mark.parametrize("five,week", [(5, 90), (90, 2), (4.9, 90), (90, 1.9), (0, 0)])
def test_handoff_at_either_window_threshold_takes_precedence(store, five, week):
    row = agent(store)
    assert quota_notice.apply("sw", store, decision(five, week)) == [f"sent {row.name} the quota handoff"]
    items = messages(store, row)
    assert len(items) == 1
    assert items[0].text == (
        "Your account is at the quota handoff threshold. Commit, push, write the Handoff v2 document, "
        "then run agentihooks swarm sw handoff <doc> --reason quota and stop. "
        "A successor on another account will take your seat."
    )


@pytest.mark.parametrize("five,week", [(15.01, 90), (90, 15.01), (None, None), (None, 90), (90, None)])
def test_healthy_and_missing_readings_send_nothing(store, five, week):
    row = agent(store)
    assert quota_notice.apply("sw", store, decision(five, week)) == []
    assert messages(store, row) == []


@pytest.mark.parametrize("five,week", [(5.01, 90), (90, 2.01)])
def test_just_above_handoff_threshold_still_only_hurries(store, five, week):
    row = agent(store)
    quota_notice.apply("sw", store, decision(five, week))
    assert [item.text for item in messages(store, row)] == [HURRY]


def test_hurry_is_once_per_life_even_after_reset_or_window_switch(store):
    row = agent(store)
    quota_notice.apply("sw", store, decision(15, 90))
    inbox = InboxStore(store.redis)
    inbox.close(messages(store, row)[0].id, row.name, "done", "pushed")
    for reading in (decision(15, 90), decision(), decision(90, 15)):
        assert quota_notice.apply("sw", store, reading) == []
    assert len(messages(store, row)) == 1
    next_life = agent(store, started_at=200)
    assert quota_notice.apply("sw", store, decision(15, 90)) == [f"sent {next_life.name} the quota hurry"]
    assert len(messages(store, row)) == 2


def test_hurry_can_escalate_once_to_handoff_and_never_hurry_after_it(store):
    row = agent(store)
    quota_notice.apply("sw", store, decision(15, 90))
    quota_notice.apply("sw", store, decision(5, 90))
    for reading in (decision(5, 90), decision(90, 2), decision(15, 90)):
        assert quota_notice.apply("sw", store, reading) == []
    assert len(messages(store, row)) == 2


@pytest.mark.parametrize(
    "fields",
    [
        {"lane": "master"},
        {"lane": "interactive"},
        {"state": "finished"},
        {"state": "starting"},
        {"account": ""},
        {"account": "other"},
        {"harness": "codex"},
    ],
)
def test_only_running_lane_agents_on_the_observed_account_are_notified(store, fields):
    row = agent(store, **fields)
    assert quota_notice.apply("sw", store, decision(0, 0)) == []
    assert messages(store, row) == []


@pytest.mark.parametrize("lane", ["eng", "ci", "plan"])
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_each_lane_and_harness_uses_its_own_account(store, lane, harness):
    row = agent(store, lane=lane, harness=harness)
    readings = decision(15, 90, harness=harness)
    readings["accounts"].append(decision(0, 0, harness="other")["accounts"][0])
    quota_notice.apply("sw", store, readings)
    assert [item.text for item in messages(store, row)] == [HURRY]


@pytest.mark.parametrize("week,expected", [(15, "hurry"), (5, "hurry"), (2, "handoff")])
def test_weekly_only_codex_does_not_reuse_week_as_five_hour_window(store, week, expected):
    row = agent(store, harness="codex")
    assert quota_notice.apply("sw", store, decision(None, week, harness="codex")) == [
        f"sent {row.name} the quota {expected}"
    ]


def test_unknown_account_readings_are_ignored(store):
    row = agent(store)
    assert quota_notice.apply("sw", store, decision(0, 0, state="UNKNOWN")) == []
    assert messages(store, row) == []


def test_failed_send_can_retry_on_next_pass(store, monkeypatch):
    row = agent(store)
    original = InboxStore.send

    def refused(*args, **kwargs):
        raise RuntimeError("offline")

    with monkeypatch.context() as context:
        context.setattr(InboxStore, "send", refused)
        with pytest.raises(RuntimeError, match="offline"):
            quota_notice.apply("sw", store, decision(15, 90))
    assert InboxStore.send is original
    quota_notice.apply("sw", store, decision(15, 90))
    assert len(messages(store, row)) == 1


def test_tick_sends_notices_from_the_just_refreshed_accounts(store, monkeypatch):
    from scripts.swarm import capacity
    from scripts.swarm.tick import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    row = agent(store)
    store.update("sw", state="paused")
    runtime = FakeRuntime()
    runtime.live.add(row.name)
    ledger = FakeLedger([{"id": "t1", "state": "claimed", "claimed_by": row.name, "difficulty": "M"}])
    readings = iter([decision(15, 90), decision(90, 2), decision(90, 2)])

    def refresh(slug, config, target, source, host, now_ms):
        assert (slug, target, source, host) == ("sw", store, ledger, runtime)
        assert now_ms in (1000, 2000, 3000)
        target.redis.set(target.key(slug, "quota-capacity"), json.dumps(next(readings)))
        return ["quota refreshed"]

    monkeypatch.setattr(capacity, "apply", refresh)
    actions = tick("sw", store, ledger, runtime, 1000)
    assert "quota refreshed" in actions
    assert f"sent {row.name} the quota hurry" in actions
    actions = tick("sw", store, ledger, runtime, 2000)
    assert f"early quota handoff warning sent to {row.name}" in actions
    actions = tick("sw", store, ledger, runtime, 3000)
    assert not any("quota handoff" in text for text in actions)
    assert len(messages(store, row)) == 2


def test_failed_capacity_refresh_never_sends_from_old_readings(store, monkeypatch):
    from scripts.swarm import capacity

    row = agent(store)
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps(decision(0, 0)))

    def refused(*args):
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(capacity, "apply", refused)
    with pytest.raises(RuntimeError, match="refresh failed"):
        quota_notice.refresh("sw", store.ensure_code("sw"), store, object(), object(), 1000)
    assert messages(store, row) == []


def test_no_observation_and_an_unregistered_interactive_session_receive_nothing(store):
    row = agent(store)
    assert quota_notice.apply("sw", store, {}) == []
    assert messages(store, row) == []
    store.drop_agent("sw", row.name)
    assert quota_notice.apply("sw", store, decision(0, 0)) == []
    assert messages(store, row) == []


def test_hurry_is_tracked_separately_for_agents_sharing_an_account(store):
    first = agent(store)
    second = agent(store, name="engineer@a1b2c3-0002", task="t2", lane="ci")
    actions = quota_notice.apply("sw", store, decision(15, 90))
    assert set(actions) == {f"sent {first.name} the quota hurry", f"sent {second.name} the quota hurry"}
    assert quota_notice.apply("sw", RedisStore(store.redis), decision(90, 15)) == []
    assert [item.text for item in messages(store, first)] == [HURRY]
    assert [item.text for item in messages(store, second)] == [HURRY]


def test_notice_state_survives_a_later_pass_in_its_swarm(store):
    row = agent(store)
    quota_notice.apply("sw", store, decision(15, 90))
    life = f"{row.name}:{row.started_at}"
    assert store.redis.hget(store.key("sw", "quota-notices"), life) == "hurry"
    quota_notice.apply("sw", store, decision(5, 90))
    assert store.redis.hget(store.key("sw", "quota-notices"), life) == "handoff"


@pytest.mark.parametrize("skip", ["master", "missing", "unknown", "healthy", "hurry", "handoff"])
def test_skipped_agent_does_not_prevent_the_next_agent_notice(store, monkeypatch, skip):
    first = agent(store, account="skip", name="skip-agent")
    last = agent(store, name="last-agent")
    reading = decision(15, 90)
    if skip == "master":
        first = replace(first, lane="master")
    elif skip != "missing":
        skipped = {
            "harness": "claude",
            "name": "skip",
            "state": "UNKNOWN" if skip == "unknown" else "OPEN",
            "five_left": 90 if skip == "healthy" else 15,
            "week_left": 90,
        }
        reading["accounts"].append(skipped)
    if skip in ("hurry", "handoff"):
        store.redis.hset(store.key("sw", "quota-notices"), f"{first.name}:{first.started_at}", skip)
    monkeypatch.setattr(store, "agents", lambda slug: [first, last])
    assert quota_notice.apply("sw", store, reading) == [f"sent {last.name} the quota hurry"]
    assert messages(store, first) == []
    assert len(messages(store, last)) == 1
