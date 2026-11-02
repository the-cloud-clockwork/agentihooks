import json
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import ci_speed
from scripts.swarm.store import PREFIX
from scripts.tick.adapters.fakes import FakeSourceControl
from scripts.tick.deadline import Deadline
from scripts.tick.ports.source_control import WorkflowRun

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
    ci_speed.read_runs("/repo", 0, by_conclusion([], calls), event="push")
    assert sorted(calls) == [
        ("test.yml", "push", conclusion, "1970-01-01T00:00:00Z") for conclusion in ("failure", "success")
    ]


def test_runs_are_read_through_the_port_of_the_swarm_checkout():
    roots = []
    provider = Runs(RUNS, [])
    rows = ci_speed.read_runs("/repo", 0, lambda root: roots.append(root) or provider)
    assert roots == [Path("/repo")]
    assert RUNS[0]["conclusion"] == "success"
    assert {"head_sha": "", "created_at": "", "run_attempt": 1, **RUNS[0]} in rows


@pytest.fixture
def swarm():
    import fakeredis

    return SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True)), SimpleNamespace(repo="/repo")


def record(row: dict[str, Any]) -> WorkflowRun:
    return WorkflowRun(**{"head_sha": "", "created_at": "", "run_attempt": 1, **row})


class Runs(FakeSourceControl):
    def __init__(self, runs: list[dict[str, Any]], reads: list[tuple[str, ...]], broken: Exception | None = None):
        super().__init__()
        self.rows, self.reads, self.broken = runs, reads, broken

    def workflow_runs(
        self, workflow: str, event: str, conclusion: str, since: str, *, deadline: Deadline
    ) -> tuple[WorkflowRun, ...]:
        assert not deadline.expired
        self.reads.append((workflow, event, conclusion, since))
        if self.broken is not None:
            raise self.broken
        return tuple(record(r) for r in self.rows if r["conclusion"] == conclusion)


def by_conclusion(
    runs: list[dict[str, Any]], reads: list[tuple[str, ...]], broken: Exception | None = None
) -> Callable[[Path], FakeSourceControl]:
    provider = Runs(runs, reads, broken)
    return lambda root: provider


def unavailable(stderr: str) -> Callable[[Path], FakeSourceControl]:
    return by_conclusion([], [], subprocess.CalledProcessError(1, ["gh"], stderr=stderr))


def read_call(conclusion: str, since: str) -> tuple[str, ...]:
    return ("test.yml", "pull_request", conclusion, since)


def test_refresh_reads_the_last_day_of_success_and_failure_runs_and_caches_the_median(swarm, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    store, config = swarm
    calls = []
    ci_speed.refresh("sw", config, store, NOW_MS, ports=by_conclusion(RUNS, calls))
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
    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS - 1, ports=by_conclusion(RUNS, calls))
    assert len(calls) == 2


def test_the_two_conclusions_are_read_at_the_same_time(swarm):
    store, config = swarm
    both = threading.Barrier(2, timeout=5)

    class Waiting(Runs):
        def workflow_runs(
            self, workflow: str, event: str, conclusion: str, since: str, *, deadline: Deadline
        ) -> tuple[WorkflowRun, ...]:
            both.wait()
            return ()

    provider = Waiting([], [])
    ci_speed.refresh("sw", config, store, NOW_MS, ports=lambda root: provider)
    assert ci_speed.get(store.redis, "sw")["tried_at"] == NOW_MS


def test_an_hourly_refresh_reads_only_since_the_last_read_less_the_overlap(swarm):
    store, config = swarm
    ci_speed.refresh("sw", config, store, NOW_MS, ports=by_conclusion(RUNS, []))
    late = {**RUNS[0], "id": 2, "run_started_at": "2026-10-08T10:30:00Z", "updated_at": "2026-10-08T11:30:00Z"}
    calls = []
    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS, ports=by_conclusion([RUNS[0], late], calls))
    assert sorted(calls) == sorted(
        [read_call("success", "2026-10-08T08:06:40Z"), read_call("failure", "2026-10-08T08:06:40Z")]
    )
    cached = ci_speed.get(store.redis, "sw")
    assert (cached["runs"], cached["at"]) == (14, NOW_MS + HOUR_MS)
    assert cached["minutes"] == pytest.approx(4.44, abs=0.01)


def test_runs_older_than_a_day_leave_the_window(swarm):
    store, config = swarm
    ci_speed.refresh("sw", config, store, NOW_MS, ports=by_conclusion(RUNS, []))
    ci_speed.refresh("sw", config, store, NOW_MS + 25 * HOUR_MS, ports=by_conclusion([], []))
    assert ci_speed.get(store.redis, "sw") == {
        "minutes": None,
        "runs": 0,
        "at": NOW_MS + 25 * HOUR_MS,
        "tried_at": NOW_MS + 25 * HOUR_MS,
        "durations": {},
    }


def test_a_run_that_started_exactly_a_day_ago_stays_in_the_window(swarm):
    store, config = swarm
    ci_speed.refresh("sw", config, store, NOW_MS, ports=by_conclusion(RUNS, []))
    edge_ms = (1791452123 + ci_speed.WINDOW_S) * 1000
    ci_speed.refresh("sw", config, store, edge_ms, ports=by_conclusion([], []))
    cached = ci_speed.get(store.redis, "sw")
    assert (cached["runs"], "37757702814" in cached["durations"]) == (13, True)


def test_a_cache_without_run_durations_reads_the_whole_day(swarm):
    store, config = swarm
    store.redis.set(ci_speed.key("sw"), json.dumps({"minutes": 9.0, "runs": 50, "at": NOW_MS, "tried_at": NOW_MS}))
    calls = []
    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS, ports=by_conclusion(RUNS, calls))
    assert set(calls) == {read_call("success", "2026-10-07T11:06:40Z"), read_call("failure", "2026-10-07T11:06:40Z")}
    assert ci_speed.get(store.redis, "sw")["runs"] == 13


def test_a_read_failure_keeps_the_last_value_and_is_logged(swarm, capsys):
    store, config = swarm
    ci_speed.refresh("sw", config, store, NOW_MS, ports=by_conclusion(RUNS, []))
    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS, ports=unavailable("API rate limit exceeded"))
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
    ci_speed.refresh("sw", config, store, NOW_MS, ports=by_conclusion(RUNS, []))
    if updated_at is None:
        broken = by_conclusion([{**RUNS[0], "updated_at": None}], [])
    else:
        broken = by_conclusion([], [], KeyError("updated_at"))
    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS, ports=broken)
    cached = ci_speed.get(store.redis, "sw")
    assert (cached["minutes"], cached["at"]) == (pytest.approx(4.43, abs=0.01), NOW_MS)
    assert cached["error"].startswith("'updated_at'" if updated_at else "fromisoformat")
    assert "ci speed kept its last value" in capsys.readouterr().err


def test_a_first_read_failure_caches_an_unknown_speed(swarm):
    store, config = swarm
    ci_speed.refresh("sw", config, store, NOW_MS, ports=unavailable("API unavailable"))
    assert ci_speed.get(store.redis, "sw") == {
        "minutes": None,
        "runs": 0,
        "at": 0,
        "tried_at": NOW_MS,
        "error": "API unavailable",
    }
