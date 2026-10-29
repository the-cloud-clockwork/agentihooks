import json

import pytest

from scripts.swarm import status
from scripts.swarm.health import standing
from scripts.swarm.health.findings import Finding
from scripts.swarm.health.verdicts import VerdictStore
from scripts.swarm.store import PREFIX
from tests.swarm.test_cli import env as env
from tests.swarm.test_spawn_stall import stalled as stalled

pytestmark = pytest.mark.unit

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
            2.5,
        )
    ]


def test_evidence_without_links_is_still_kept_on_the_finding(redis):
    found = standing.Reading(2.5, 1.0, "task hours in the window", "e1", evidence=("mutation: 1 run over budget.",))
    standing.fire(redis, "sw", "ci_overrun", found, NOW)
    row = standing.read(redis, "sw")["ci_overrun"]
    assert (row["evidence"], row["links"]) == (["mutation: 1 run over budget."], [])


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
    assert (found.measure, found.evidence) == (2.5, ("Fired 3 times",))
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
        ("ci_overrun", 2.0),
        ("merge_queue", 1.5),
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


def _judged_then_refired(redis, value, episode="e2"):
    store = VerdictStore(redis, "verdicts")
    standing.fire(redis, "sw", "ci_overrun", reading(0.89, episode="e1"), NOW)
    store.visible(standing.findings(redis, "sw", NOW), NOW, HOUR_MS)
    store.judge("bottleneck-stage/ci_overrun", "established", "known", "master", NOW)
    later = NOW + HOUR_MS
    standing.fire(redis, "sw", "ci_overrun", reading(value, episode=episode), later)
    return store.visible(standing.findings(redis, "sw", later), later, HOUR_MS)


def test_a_judged_finding_whose_load_fell_stays_hidden_though_it_fired_again(redis):
    assert _judged_then_refired(redis, 0.097) == []


def test_a_judged_finding_whose_load_rose_past_its_verdict_returns(redis):
    (shown,) = _judged_then_refired(redis, 1.2)
    assert (shown["id"], shown["verdict"]["value"]) == ("bottleneck-stage/ci_overrun", "established")


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


MINUTES = "minutes from push to Gate Required"


def _detected(redis, hours, episode, now):
    row = {"stage": "ci_overrun", "seconds": hours * 3600, "previous_seconds": 0.0, "trend": "rising"}
    standing.update_detector(redis, "sw", {**row, "episodes": [episode], "labels": [], "label": ""}, now, 0.25)


def _overran(redis, minutes, episode, now):
    standing.fire(redis, "sw", "ci_overrun", reading(minutes, episode=episode, threshold=15.0, unit=MINUTES), now)


def _returns(store, redis, now):
    return [row["id"] for row in store.visible(standing.findings(redis, "sw", now), now, HOUR_MS)]


def _judge(store, redis, now):
    store.visible(standing.findings(redis, "sw", now), now, HOUR_MS)
    store.judge("bottleneck-stage/ci_overrun", "established", "known", "master", now)


def test_a_judged_detector_finding_does_not_return_when_the_ci_budget_rewrites_it_in_minutes(redis):
    store = VerdictStore(redis, "verdicts")
    _detected(redis, 0.5, "d1", NOW)
    _judge(store, redis, NOW)
    later = NOW + 2 * HOUR_MS
    _overran(redis, 22.3, "run-1", later)
    assert _returns(store, redis, later) == [], "returned on the change from task hours to minutes alone"
    _overran(redis, 22.0, "run-2", later + HOUR_MS)
    assert _returns(store, redis, later + HOUR_MS) == []
    _overran(redis, 31.0, "run-3", later + 2 * HOUR_MS)
    assert _returns(store, redis, later + 2 * HOUR_MS) == ["bottleneck-stage/ci_overrun"]


def test_a_judged_ci_budget_finding_does_not_return_when_the_detector_recreates_it_in_task_hours(redis):
    store = VerdictStore(redis, "verdicts")
    _overran(redis, 18.0, "run-1", NOW)
    _judge(store, redis, NOW)
    later = NOW + 2 * HOUR_MS
    _detected(redis, 0.4, "d1", later)
    assert standing.read(redis, "sw")["ci_overrun"]["unit"] == MINUTES
    assert _returns(store, redis, later) == []
    standing.clear(redis, "sw", "ci_overrun")
    _detected(redis, 0.3, "d2", later + HOUR_MS)
    assert _returns(store, redis, later + HOUR_MS) == [], "returned on the change from minutes to task hours alone"
    _detected(redis, 0.9, "d3", later + 2 * HOUR_MS)
    assert _returns(store, redis, later + 2 * HOUR_MS) == ["bottleneck-stage/ci_overrun"]


def test_the_detector_does_not_keep_an_old_ci_budget_run_time_fresh(redis):
    _overran(redis, 17.4, "run-1", NOW)
    _detected(redis, 0.4, "d1", NOW + 5 * HOUR_MS)
    row = standing.read(redis, "sw")["ci_overrun"]
    assert (row["value"], row["unit"], row["updated_at"]) == (17.4, MINUTES, NOW)
    assert standing.findings(redis, "sw", NOW + standing.STALE_MS) == []


def test_the_detector_refreshes_an_alert_counted_in_the_same_task_hours(redis):
    standing.fire(redis, "sw", "ci_overrun", reading(1.5, episode="alert-1"), NOW)
    _detected(redis, 0.4, "d1", NOW + HOUR_MS)
    assert standing.read(redis, "sw")["ci_overrun"]["updated_at"] == NOW + HOUR_MS


def test_the_detector_takes_over_a_stale_ci_budget_finding_in_task_hours(redis):
    standing.fire(redis, "sw", "ci_overrun", reading(17.4, "run-1", "Slowest stage mutation", 15.0, MINUTES), NOW)
    later = NOW + standing.STALE_MS
    _detected(redis, 0.4, "d1", later)
    row = standing.read(redis, "sw")["ci_overrun"]
    assert (row["value"], row["unit"], row["detail"], row["updated_at"]) == (0.4, "task hours", "", later)
    assert [found.measure for found in standing.findings(redis, "sw", later)] == [0.4]


def _legacy(redis, fired):
    verdict = {"value": "established", "note": "known", "by": "master", "at": NOW, "measure": fired}
    evidence = [f"Fired {fired} times"]
    record = {"seen_at": NOW, "verdict": {**verdict, "evidence": evidence}, "returned": False}
    redis.hset(
        "verdicts", "bottleneck-stage/ci_overrun", json.dumps({**record, "evidence": evidence, "measure": fired})
    )


def test_a_fired_count_verdict_does_not_return_on_a_larger_value_in_minutes(redis):
    store = VerdictStore(redis, "verdicts")
    _legacy(redis, 3)
    later = NOW + 2 * HOUR_MS
    _overran(redis, 22.3, "run-1", later)
    assert _returns(store, redis, later) == [], "returned on the change from fired count to minutes alone"
    _overran(redis, 31.0, "run-2", later + 2 * HOUR_MS)
    assert _returns(store, redis, later + 2 * HOUR_MS) == ["bottleneck-stage/ci_overrun"]


def test_a_fired_count_verdict_still_lets_a_task_hours_regression_return(redis):
    store = VerdictStore(redis, "verdicts")
    _legacy(redis, 5)
    later = NOW + 2 * HOUR_MS
    _detected(redis, 0.5, "d1", later)
    assert _returns(store, redis, later) == []
    _detected(redis, 2.0, "d2", later + 2 * HOUR_MS)
    assert _returns(store, redis, later + 2 * HOUR_MS) == ["bottleneck-stage/ci_overrun"], (
        "a regression in task hours stayed hidden under a larger fired count"
    )
    record = json.loads(redis.hget("verdicts", "bottleneck-stage/ci_overrun"))
    assert {k: record["verdict"][k] for k in ("value", "note", "by", "at")} == {
        "value": "established",
        "note": "known",
        "by": "master",
        "at": NOW,
    }


def test_an_alert_and_the_detector_count_the_same_task_hours(redis):
    store = VerdictStore(redis, "verdicts")
    standing.fire(redis, "sw", "merge_queue", reading(1.5, episode="alert-1"), NOW)
    store.visible(standing.findings(redis, "sw", NOW), NOW, HOUR_MS)
    store.judge("bottleneck-stage/merge_queue", "established", "known", "master", NOW)
    standing.clear(redis, "sw", "merge_queue")
    later = NOW + 2 * HOUR_MS
    row = {"stage": "merge_queue", "seconds": 2.5 * 3600, "previous_seconds": 0.0, "trend": "rising"}
    standing.update_detector(redis, "sw", {**row, "episodes": ["d1"], "labels": [], "label": ""}, later, 1.0)
    shown = store.visible(standing.findings(redis, "sw", later), later, HOUR_MS)
    assert [found["id"] for found in shown] == ["bottleneck-stage/merge_queue"]


def test_a_judged_verdict_keeps_its_basis_and_each_rebase_it_went_through(redis):
    store = VerdictStore(redis, "verdicts")
    _detected(redis, 0.5, "d1", NOW)
    _judge(store, redis, NOW)
    record = json.loads(redis.hget("verdicts", "bottleneck-stage/ci_overrun"))
    assert (record["basis"], record["verdict"]["basis"], record["verdict"]["measure"]) == (
        "task hours",
        "task hours",
        0.5,
    )
    _overran(redis, 22.3, "run-1", NOW + HOUR_MS)
    _returns(store, redis, NOW + HOUR_MS)
    verdict = json.loads(redis.hget("verdicts", "bottleneck-stage/ci_overrun"))["verdict"]
    assert (verdict["measure"], verdict["basis"]) == (22.3, MINUTES)
    assert verdict["rebases"] == [{"measure": 0.5, "basis": "task hours", "at": NOW + HOUR_MS}]


def test_a_fired_count_verdict_keeps_every_rebase_with_its_empty_basis(redis):
    store = VerdictStore(redis, "verdicts")
    _legacy(redis, 3)
    later = NOW + HOUR_MS
    _overran(redis, 22.3, "run-1", later)
    _returns(store, redis, later)
    standing.clear(redis, "sw", "ci_overrun")
    _detected(redis, 0.3, "d1", later + HOUR_MS)
    _returns(store, redis, later + HOUR_MS)
    verdict = json.loads(redis.hget("verdicts", "bottleneck-stage/ci_overrun"))["verdict"]
    assert verdict["rebases"] == [
        {"measure": 3, "basis": "", "at": later},
        {"measure": 22.3, "basis": MINUTES, "at": later + HOUR_MS},
    ]


def test_the_measure_basis_is_left_out_of_equality_and_the_shown_finding():
    hours = Finding("bottleneck stage", "ci_overrun", "s", (), "t", 1.0, "task hours")
    minutes = Finding("bottleneck stage", "ci_overrun", "s", (), "t", 1.0, MINUTES)
    assert hours == minutes
    assert "basis" not in hours.as_dict()
