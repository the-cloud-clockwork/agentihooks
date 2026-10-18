import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm import ci_speed
from scripts.swarm.store import PREFIX

pytestmark = pytest.mark.xdist_group("fakeredis")

RUNS = json.loads((Path(__file__).resolve().parent.parent / "fixtures" / "ci_speed_runs.json").read_text())
NOW_MS = 1_791_454_000_000
HOUR_MS = 3_600_000
CI_SPEED_READ = True


def test_the_cache_key_sits_under_the_swarm_prefix():
    assert ci_speed.key("sw") == f"{PREFIX}:sw:ci-speed"


def test_median_counts_finished_pull_request_runs_into_dev_from_recorded_data():
    release = {**RUNS[0], "id": 1, "head_branch": "dev", "updated_at": "2026-10-08T12:00:00Z"}
    assert len(ci_speed.finished([*RUNS, release])) == 13
    assert ci_speed.median_minutes(ci_speed.durations([*RUNS, release])) == 4.43


def test_median_is_none_without_finished_runs():
    assert ci_speed.median_minutes(ci_speed.durations([r for r in RUNS if r["conclusion"] == "cancelled"])) is None


def test_runs_of_another_event_are_read_on_request():
    calls = []
    ci_speed.read_runs("/repo", 0, gh("", calls), event="push")
    assert sorted(argv[2] for argv, _ in calls) == [
        endpoint(conclusion, "1970-01-01T00:00:00Z").replace("event=pull_request", "event=push")
        for conclusion in ("failure", "success")
    ]


@pytest.fixture
def swarm():
    import fakeredis

    return SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True)), SimpleNamespace(repo="/repo")


def gh(output, calls):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, output, "")

    return run


def by_conclusion(runs, calls):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        wanted = argv[2].split("status=")[1].split("&")[0]
        output = "\n".join(json.dumps(r) for r in runs if r["conclusion"] == wanted)
        return subprocess.CompletedProcess(argv, 0, output, "")

    return run


def endpoint(conclusion, since):
    return (
        "repos/{owner}/{repo}/actions/workflows/test.yml/runs"
        f"?event=pull_request&status={conclusion}&exclude_pull_requests=true&created=>={since}&per_page=100"
    )


def read_call(conclusion, since):
    return (
        [
            "gh",
            "api",
            endpoint(conclusion, since),
            "--paginate",
            "--jq",
            ".workflow_runs[] | {id, event, status, conclusion, head_branch, head_sha, created_at, run_started_at,"
            " updated_at, run_attempt} | @json",
        ],
        {"cwd": "/repo", "capture_output": True, "text": True, "check": True, "timeout": 60},
    )


def test_refresh_reads_the_last_day_of_success_and_failure_runs_and_caches_the_median(swarm, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    store, config = swarm
    calls = []
    ci_speed.refresh("sw", config, store, NOW_MS, run=by_conclusion(RUNS, calls))
    monkeypatch.delenv("TZ")
    time.tzset()
    assert sorted(calls) == sorted(
        [read_call("success", "2026-10-07T10:06:40Z"), read_call("failure", "2026-10-07T10:06:40Z")]
    )
    cached = ci_speed.get(store.redis, "sw")
    assert (cached["minutes"], cached["runs"], cached["at"], cached["tried_at"]) == (
        pytest.approx(4.43, abs=0.01),
        13,
        NOW_MS,
        NOW_MS,
    )
    assert cached["durations"]["37757702814"] == [pytest.approx(1791452123), 354]
    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS - 1, run=by_conclusion(RUNS, calls))
    assert len(calls) == 2


def test_the_two_conclusions_are_read_at_the_same_time(swarm):
    store, config = swarm
    both = threading.Barrier(2, timeout=5)

    def run(argv, **kwargs):
        both.wait()
        return subprocess.CompletedProcess(argv, 0, "", "")

    ci_speed.refresh("sw", config, store, NOW_MS, run=run)
    assert ci_speed.get(store.redis, "sw")["tried_at"] == NOW_MS


def test_an_hourly_refresh_reads_only_since_the_last_read_less_the_overlap(swarm):
    store, config = swarm
    ci_speed.refresh("sw", config, store, NOW_MS, run=by_conclusion(RUNS, []))
    late = {**RUNS[0], "id": 2, "run_started_at": "2026-10-08T10:30:00Z", "updated_at": "2026-10-08T11:30:00Z"}
    calls = []
    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS, run=by_conclusion([RUNS[0], late], calls))
    assert sorted(calls) == sorted(
        [read_call("success", "2026-10-08T08:06:40Z"), read_call("failure", "2026-10-08T08:06:40Z")]
    )
    cached = ci_speed.get(store.redis, "sw")
    assert (cached["runs"], cached["at"]) == (14, NOW_MS + HOUR_MS)
    assert cached["minutes"] == pytest.approx(4.44, abs=0.01)


def test_runs_older_than_a_day_leave_the_window(swarm):
    store, config = swarm
    ci_speed.refresh("sw", config, store, NOW_MS, run=by_conclusion(RUNS, []))
    ci_speed.refresh("sw", config, store, NOW_MS + 25 * HOUR_MS, run=gh("", []))
    assert ci_speed.get(store.redis, "sw") == {
        "minutes": None,
        "runs": 0,
        "at": NOW_MS + 25 * HOUR_MS,
        "tried_at": NOW_MS + 25 * HOUR_MS,
        "durations": {},
    }


def test_a_run_that_started_exactly_a_day_ago_stays_in_the_window(swarm):
    store, config = swarm
    ci_speed.refresh("sw", config, store, NOW_MS, run=by_conclusion(RUNS, []))
    edge_ms = (1791452123 + ci_speed.WINDOW_S) * 1000
    ci_speed.refresh("sw", config, store, edge_ms, run=gh("", []))
    cached = ci_speed.get(store.redis, "sw")
    assert (cached["runs"], "37757702814" in cached["durations"]) == (13, True)


def test_a_cache_without_run_durations_reads_the_whole_day(swarm):
    store, config = swarm
    store.redis.set(ci_speed.key("sw"), json.dumps({"minutes": 9.0, "runs": 50, "at": NOW_MS, "tried_at": NOW_MS}))
    calls = []
    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS, run=by_conclusion(RUNS, calls))
    assert {call[0][2] for call in calls} == {
        endpoint("success", "2026-10-07T11:06:40Z"),
        endpoint("failure", "2026-10-07T11:06:40Z"),
    }
    assert ci_speed.get(store.redis, "sw")["runs"] == 13


def test_a_read_failure_keeps_the_last_value_and_is_logged(swarm, capsys):
    store, config = swarm
    ci_speed.refresh("sw", config, store, NOW_MS, run=gh("\n".join(json.dumps(r) for r in RUNS), []))

    def unavailable(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr="API rate limit exceeded")

    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS, run=unavailable)
    cached = ci_speed.get(store.redis, "sw")
    assert (cached["minutes"], cached["at"], cached["tried_at"]) == (
        pytest.approx(4.43, abs=0.01),
        NOW_MS,
        NOW_MS + HOUR_MS,
    )
    assert "API rate limit exceeded" in cached["error"]
    assert "ci speed kept its last value" in capsys.readouterr().err


@pytest.mark.parametrize("updated_at", ["missing", None])
def test_a_malformed_run_record_keeps_the_last_value(swarm, capsys, updated_at):
    store, config = swarm
    ci_speed.refresh("sw", config, store, NOW_MS, run=gh("\n".join(json.dumps(r) for r in RUNS), []))
    broken = {k: v for k, v in RUNS[0].items() if k != "updated_at"}
    if updated_at is None:
        broken["updated_at"] = None
    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS, run=gh(json.dumps(broken), []))
    cached = ci_speed.get(store.redis, "sw")
    assert (cached["minutes"], cached["at"]) == (pytest.approx(4.43, abs=0.01), NOW_MS)
    assert cached["error"].startswith("'updated_at'" if updated_at else "fromisoformat")
    assert "ci speed kept its last value" in capsys.readouterr().err


def test_a_first_read_failure_caches_an_unknown_speed(swarm):
    store, config = swarm

    def unavailable(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr="API unavailable")

    ci_speed.refresh("sw", config, store, NOW_MS, run=unavailable)
    assert ci_speed.get(store.redis, "sw") == {
        "minutes": None,
        "runs": 0,
        "at": 0,
        "tried_at": NOW_MS,
        "error": "API unavailable",
    }
