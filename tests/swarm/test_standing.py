import pytest

from scripts.swarm import status
from scripts.swarm.health import standing
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import PREFIX
from tests.swarm.test_cli import env as env
from tests.swarm.test_spawn_stall import stalled as stalled

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

NOW = 1_791_700_000_000
HOUR_MS = 3_600_000


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def reading(value=2.5, episode="e1", detail="", threshold=1.0, unit="task hours in the window"):
    return standing.Reading(value, threshold, unit, episode, detail)


def test_the_key_sits_under_the_swarm_prefix():
    assert standing.key("sw") == f"{PREFIX}:sw:standing-findings"


def test_a_first_reading_creates_one_finding_with_its_numbers(redis):
    row = standing.fire(redis, "sw", "ci_overrun", reading(2.5, detail="Slowest job mutation"), NOW)
    assert row == {
        "stage": "ci_overrun",
        "value": 2.5,
        "threshold": 1.0,
        "unit": "task hours in the window",
        "detail": "Slowest job mutation",
        "fired": 1,
        "episodes": ["e1"],
        "first_at": NOW,
        "updated_at": NOW,
    }
    assert standing.findings(redis, "sw", NOW) == [
        Finding(
            "bottleneck stage",
            "ci_overrun",
            "ci overrun at 2.5 task hours in the window, over its 1.0 threshold",
            ("Fired 1 time", "Slowest job mutation"),
            "1.0 task hours in the window",
            1,
        )
    ]


def test_a_repeat_of_the_same_episode_updates_numbers_without_counting_again(redis):
    standing.fire(redis, "sw", "ci_overrun", reading(2.5), NOW)
    row = standing.fire(redis, "sw", "ci_overrun", reading(3.0), NOW + 60_000)
    assert (row["value"], row["fired"], row["first_at"], row["updated_at"]) == (3.0, 1, NOW, NOW + 60_000)
    (found,) = standing.findings(redis, "sw", NOW + 60_000)
    assert found.summary == "ci overrun at 3.0 task hours in the window, over its 1.0 threshold"


def test_a_new_episode_counts_once_and_stays_one_finding(redis):
    for episode in ("e1", "e2", "e3", "e2"):
        standing.fire(redis, "sw", "ci_overrun", reading(episode=episode), NOW)
    (found,) = standing.findings(redis, "sw", NOW)
    assert (found.measure, found.evidence) == (3, ("Fired 3 times",))
    assert standing.read(redis, "sw")["ci_overrun"]["episodes"] == ["e1", "e2", "e3"]


def test_only_the_latest_episodes_are_kept(redis):
    for n in range(standing.EPISODES + 2):
        standing.fire(redis, "sw", "ci_overrun", reading(episode=f"e{n}"), NOW)
    row = standing.read(redis, "sw")["ci_overrun"]
    assert row["episodes"] == [f"e{n}" for n in range(2, standing.EPISODES + 2)]
    assert row["fired"] == standing.EPISODES + 2


def test_each_stage_and_ledger_is_its_own_finding(redis):
    standing.fire(redis, "sw", "merge_queue", reading(1.5), NOW)
    standing.fire(redis, "sw", "ci_overrun", reading(2.0), NOW)
    standing.fire(redis, "other", "ci_overrun", reading(9.0), NOW)
    assert [(f.subject, f.measure) for f in standing.findings(redis, "sw", NOW)] == [
        ("ci_overrun", 1),
        ("merge_queue", 1),
    ]
    assert [f.id for f in standing.findings(redis, "other", NOW)] == ["bottleneck-stage/ci_overrun"]


def test_a_clear_removes_only_that_stage(redis):
    standing.fire(redis, "sw", "merge_queue", reading(), NOW)
    standing.fire(redis, "sw", "ci_overrun", reading(), NOW)
    assert standing.clear(redis, "sw", "ci_overrun") is True
    assert standing.clear(redis, "sw", "ci_overrun") is False
    assert [f.subject for f in standing.findings(redis, "sw", NOW)] == ["merge_queue"]


def test_a_finding_not_updated_within_the_stale_window_is_not_shown(redis):
    standing.fire(redis, "sw", "ci_overrun", reading(), NOW)
    assert standing.STALE_MS == 6 * HOUR_MS
    assert len(standing.findings(redis, "sw", NOW + standing.STALE_MS - 1)) == 1
    assert standing.findings(redis, "sw", NOW + standing.STALE_MS) == []


def test_an_empty_ledger_has_no_standing_findings(redis):
    assert standing.read(redis, "sw") == {}
    assert standing.findings(redis, "sw", NOW) == []


def test_swarm_status_shows_the_standing_finding(stalled):
    store, ledger, _, clock, _ = stalled
    standing.fire(store.redis, "sw", "ci_overrun", reading(2.5, detail="Slowest job mutation"), clock[0])
    rows = status.findings(store, "sw", store.config("sw"), ledger.tasks("sw"), [])
    (found,) = [row for row in rows if row["kind"] == "bottleneck stage"]
    assert found == {
        "kind": "bottleneck stage",
        "subject": "ci_overrun",
        "summary": "ci overrun at 2.5 task hours in the window, over its 1.0 threshold",
        "evidence": ["Fired 1 time", "Slowest job mutation"],
        "threshold": "1.0 task hours in the window",
        "id": "bottleneck-stage/ci_overrun",
        "verdict": None,
        "seen_at": clock[0],
    }
