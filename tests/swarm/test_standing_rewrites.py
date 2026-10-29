import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import ledger_events, status
from scripts.swarm.health import standing
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

NOW = 1_800_000_000_000
HOUR = 3_600_000


@pytest.fixture
def store():
    import fakeredis

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1))
    result.put_agent("sw", AgentRecord("master", MASTER, MASTER, seat="master@sw"))
    return result


def rewrite(store, stage, value, at):
    standing.clear(store.redis, "sw", stage)
    unit = "minutes" if stage == "ci_overrun" else "task hours"
    threshold = 15 if stage == "ci_overrun" else 0.1
    standing.fire(store.redis, "sw", stage, standing.Reading(value, threshold, unit, str(at), "slow stage"), at)


def notify(store, at):
    found = standing.findings(store.redis, "sw", at)
    shown = status.verdict_store(store, "sw").visible(found, at, HOUR)
    ledger_events.findings_pass(InboxStore(store.redis), store, "sw", shown)
    return shown


@pytest.mark.parametrize(("stage", "initial", "smaller"), [("review", 2.0, 1.0), ("ci_overrun", 16.5, 16.0)])
def test_rewriting_a_judged_row_keeps_its_cooldown_and_load_baseline(store, stage, initial, smaller):
    rewrite(store, stage, initial, NOW)
    [first] = notify(store, NOW)
    verdict = status.verdict_store(store, "sw").judge(first["id"], "established", "known", "master", NOW)
    for offset, value in [(1, smaller), (HOUR // 2, initial), (HOUR, smaller), (HOUR + 1, initial)]:
        rewrite(store, stage, value, NOW + offset)
        assert notify(store, NOW + offset) == []
    [notice] = InboxStore(store.redis).inbox("master@sw")
    assert f"verdict {first['id']}" in notice.text
    assert verdict["measure"] == initial


@pytest.mark.parametrize(("stage", "initial", "grown"), [("review", 2.0, 3.0), ("ci_overrun", 16.5, 18.0)])
def test_rewriting_a_judged_row_with_grown_load_notifies_once_after_cooldown(store, stage, initial, grown):
    rewrite(store, stage, initial, NOW)
    [first] = notify(store, NOW)
    status.verdict_store(store, "sw").judge(first["id"], "early-real", "known", "master", NOW)
    rewrite(store, stage, grown, NOW + HOUR - 1)
    assert notify(store, NOW + HOUR - 1) == []
    [returned] = notify(store, NOW + HOUR)
    assert returned["id"] == first["id"]
    assert returned["seen_at"] == NOW
    assert returned["verdict"] == {"value": "early-real", "note": "known", "by": "master", "at": NOW}
    for offset in (HOUR + 1, HOUR + 2):
        rewrite(store, stage, grown, NOW + offset)
        notify(store, NOW + offset)
    notices = InboxStore(store.redis).inbox("master@sw")
    assert len(notices) == 2
    assert all(f"verdict {first['id']}" in notice.text for notice in notices)


def test_repeated_ci_budget_row_rewrites_with_identical_minutes_raise_nothing(store):
    rewrite(store, "ci_overrun", 16.5, NOW)
    [first] = notify(store, NOW)
    status.verdict_store(store, "sw").judge(first["id"], "established", "known", "master", NOW)
    for offset in (60_000, 20 * 60_000, 40 * 60_000, HOUR):
        rewrite(store, "ci_overrun", 16.5, NOW + offset)
        assert notify(store, NOW + offset) == []
    assert len(InboxStore(store.redis).inbox("master@sw")) == 1
