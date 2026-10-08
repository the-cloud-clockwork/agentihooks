import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import pytest

from scripts.swarm import ci_speed

RUNS = json.loads((Path(__file__).resolve().parent.parent / "fixtures" / "ci_speed_runs.json").read_text())
NOW_MS = 1_791_454_000_000
HOUR_MS = 3_600_000
CI_SPEED_READ = True


def test_median_counts_finished_pull_request_runs_into_dev_from_recorded_data():
    release = {**RUNS[0], "id": 1, "head_branch": "dev", "updated_at": "2026-10-08T12:00:00Z"}
    assert ci_speed.median_minutes([*RUNS, release]) == pytest.approx(266 / 60, abs=0.01)


def test_median_is_none_without_finished_runs():
    assert ci_speed.median_minutes([r for r in RUNS if r["conclusion"] == "cancelled"]) is None


@pytest.fixture
def swarm():
    return SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True)), SimpleNamespace(repo="/repo")


def gh(output, calls):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, output, "")

    return run


def test_refresh_reads_the_last_day_of_test_runs_and_caches_the_median(swarm):
    store, config = swarm
    calls = []
    output = "\n".join(json.dumps(r) for r in RUNS)
    ci_speed.refresh("sw", config, store, NOW_MS, run=gh(output, calls))
    argv, kwargs = calls[0]
    assert argv[:2] == ["gh", "api"] and "--paginate" in argv
    assert "actions/workflows/test.yml/runs?event=pull_request&status=completed&created=>=2026-10-07T" in argv[2]
    assert kwargs["cwd"] == "/repo"
    assert ci_speed.get(store.redis, "sw") == {
        "minutes": pytest.approx(4.43, abs=0.01),
        "runs": 13,
        "at": NOW_MS,
        "tried_at": NOW_MS,
    }
    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS - 1, run=gh(output, calls))
    assert len(calls) == 1
    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS, run=gh("", calls))
    assert len(calls) == 2
    assert ci_speed.get(store.redis, "sw")["minutes"] is None


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


def test_a_malformed_run_record_keeps_the_last_value(swarm, capsys):
    store, config = swarm
    ci_speed.refresh("sw", config, store, NOW_MS, run=gh("\n".join(json.dumps(r) for r in RUNS), []))
    broken = {k: v for k, v in RUNS[0].items() if k != "updated_at"}
    ci_speed.refresh("sw", config, store, NOW_MS + HOUR_MS, run=gh(json.dumps(broken), []))
    cached = ci_speed.get(store.redis, "sw")
    assert (cached["minutes"], cached["at"]) == (pytest.approx(4.43, abs=0.01), NOW_MS)
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
