import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.doctor import ci_speed, ci_speed_read, cli
from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def skipped_run():
    return {
        "id": 39282034448,
        "run_attempt": 1,
        "event": "merge_group",
        "path": ".github/workflows/test.yml",
        "status": "completed",
        "conclusion": "success",
        "run_started_at": "2026-10-19T16:08:31Z",
        "jobs": [
            {
                "id": 118161488992,
                "name": "shard-check (3.11)",
                "status": "completed",
                "conclusion": "success",
                "labels": [],
                "created_at": "2026-10-19T16:08:31Z",
                "started_at": "2026-10-19T16:08:37Z",
                "completed_at": "2026-10-19T16:09:03Z",
            },
            {
                "id": 118171818682,
                "name": "stage-budget",
                "status": "completed",
                "conclusion": "skipped",
                "labels": [],
                "created_at": "2026-10-19T16:16:03Z",
                "started_at": "2026-10-19T16:16:03Z",
                "completed_at": "2026-10-19T16:09:03Z",
            },
        ],
    }


def crossings(run):
    return [
        finding for finding in ci_speed.findings([run], 0, 1792428000000) if finding.kind == "ci skipped record lag"
    ]


def test_seven_minute_skipped_record_lag_names_the_run_job_and_clock(skipped_run):
    [finding] = crossings(skipped_run)
    assert finding.subject == "39282034448-1"
    assert finding.measure == 420
    assert finding.summary == "stage-budget skipped record appeared 7.00 minutes after its skip decision"
    assert finding.evidence == (
        "run 39282034448 attempt 1, job stage-budget, started_at 2026-10-19T16:16:03Z, "
        "skip decision completed_at 2026-10-19T16:09:03Z, lag 420 seconds",
    )
    assert finding.threshold == "skipped record lag over 5 minutes"


@pytest.mark.parametrize("seconds,expected", [(60, 0), (300, 0), (300.001, 1), (420, 1)])
def test_skipped_record_lag_crosses_only_above_five_minutes(skipped_run, seconds, expected):
    visible = datetime(2026, 10, 19, 16, 9, 3, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    skipped_run["jobs"][1].update(started_at=visible.isoformat(), created_at=visible.isoformat())
    assert len(crossings(skipped_run)) == expected


@pytest.mark.parametrize("event", ["push", "pull_request", "workflow_dispatch"])
def test_other_events_raise_no_skipped_record_lag(skipped_run, event):
    skipped_run["event"] = event
    assert crossings(skipped_run) == []


@pytest.mark.parametrize("change", [{"conclusion": "success"}, {"started_at": None}, {"completed_at": None}])
def test_only_skipped_records_with_both_timestamps_are_compared(skipped_run, change):
    skipped_run["jobs"][1].update(change)
    assert crossings(skipped_run) == []


def test_multiple_late_skips_raise_one_finding_for_the_worst_lag(skipped_run):
    other = copy.deepcopy(skipped_run["jobs"][1])
    other.update(id=118171810780, name="sonar", completed_at="2026-10-19T16:10:03Z")
    skipped_run["jobs"].append(other)
    [finding] = crossings(skipped_run)
    assert finding.measure == 420
    assert "stage-budget" in finding.summary


def test_timer_delivers_one_skipped_record_finding_per_run_to_both_masters(skipped_run, monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("skip-doctor", "/repo", 1, 0, state="drained", template="doctor"))
    store.set_peer("skip-doctor", "skip-watched")
    monkeypatch.setattr(cli.detect, "readers", lambda *args, **kwargs: {})
    snapshot = {"complete": True, "runs": [skipped_run], "api_calls": 2, "discovered_runs": 1}
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: snapshot)
    cli.timer(store, "skip-doctor", 1792428000000, False)
    cli.timer(store, "skip-doctor", 1792428060000, False)
    for slug in ("skip-doctor", "skip-watched"):
        [item] = InboxStore(store.redis).inbox(f"master@{slug}")
        assert "stage-budget" in item.text
        assert "420 seconds" in item.text
    saved = json.loads(
        store.redis.hget(store.key("skip-doctor", "doctor-findings"), "ci-skipped-record-lag/39282034448-1")
    )
    assert saved["measure"] == 420
