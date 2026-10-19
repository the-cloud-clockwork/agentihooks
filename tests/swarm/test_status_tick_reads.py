import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import ledger_events, status
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig


@pytest.mark.parametrize(
    "results,pending,passing",
    [
        ([], False, False),
        (["SUCCESS"], False, True),
        (["SUCCESS", "SKIPPED"], False, True),
        (["SUCCESS", "NEUTRAL"], False, True),
        (["SKIPPED"], False, False),
        (["FAILURE"], False, False),
        (["SUCCESS", None], True, False),
        (["FAILURE", "PENDING"], True, False),
        (["EXPECTED"], True, False),
    ],
)
@pytest.mark.parametrize("result_key", ["conclusion", "state"])
def test_pull_snapshot_retains_pending_and_passing_check_states(results, pending, passing, result_key):
    raw = {
        "state": "OPEN",
        "commits": [],
        "statusCheckRollup": [{"name": f"check_{index}", result_key: result} for index, result in enumerate(results)],
    }
    found = ledger_events.pull_request(raw)
    assert (found.checks_pending, found.checks_passed) == (pending, passing)


@pytest.mark.parametrize(
    "states,pending,passing",
    [
        (["SUCCESS"], False, True),
        (["PENDING"], True, False),
        (["EXPECTED"], True, False),
        (["SUCCESS", "ERROR"], False, False),
        (["SUCCESS", "PENDING"], True, False),
    ],
)
def test_legacy_status_contexts_retain_check_state_flags(states, pending, passing):
    raw = {
        "state": "OPEN",
        "commits": [],
        "statusCheckRollup": [{"context": f"legacy_{i}", "state": value} for i, value in enumerate(states)],
    }
    found = ledger_events.pull_request(raw)
    assert (found.checks_pending, found.checks_passed) == (pending, passing)


@pytest.mark.parametrize(
    "pending,passing,autonomy,idle",
    [
        (True, False, "full", False),
        (False, True, "full", True),
        (False, True, "assist", False),
        (False, False, "full", True),
    ],
)
def test_tick_findings_preserve_check_waits_without_reading_github(monkeypatch, pending, passing, autonomy, idle):
    import fakeredis

    now = 2_000_000
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, autonomy=autonomy)
    store.create(config)
    store.put_agent("sw", AgentRecord("eng-1@sw", "eng", "t1", started_at=now, idle_ticks=4))
    store.redis.set(store.key("sw", "merge-stall"), json.dumps({"tried": now, "queue": 0}))
    monkeypatch.setattr(status, "now_ms", lambda: now)

    def forbidden(*args, **kwargs):
        raise AssertionError("Findings started another GitHub read after the tick had already read its checks")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    tasks = [{"id": "t1", "title": "Deliver checks", "state": "pr", "pr_url": "https://github.com/o/r/pull/1"}]
    snapshot = SimpleNamespace(checks_pending=pending, checks_passed=passing)

    found = status.findings(store, "sw", config, tasks, [], view=lambda url: snapshot)

    assert any(row["kind"] == "idle with claim" for row in found) is idle


def test_cli_findings_reuse_existing_scoped_check_cache_without_an_injected_view(monkeypatch):
    import fakeredis

    now = 2_000_000
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, autonomy="full")
    store.create(config)
    store.put_agent("sw", AgentRecord("eng-1@sw", "eng", "t1", started_at=now, idle_ticks=4))
    url = "https://github.com/o/r/pull/1"
    prefix = store.key("sw", "checks")
    store.redis.set(f"{prefix}:{url}", "1")
    store.redis.set(f"{prefix}:green:{url}", "0")
    store.redis.set(store.key("sw", "merge-stall"), json.dumps({"tried": now, "queue": 0}))
    monkeypatch.setattr(status, "now_ms", lambda: now)

    def forbidden(*args, **kwargs):
        raise AssertionError("Known scoped checks were read again")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    tasks = [{"id": "t1", "title": "Deliver checks", "state": "pr", "pr_url": url}]
    assert not any(row["kind"] == "idle with claim" for row in status.findings(store, "sw", config, tasks, []))


def test_slow_merge_observation_is_bounded_and_keeps_the_existing_finding(monkeypatch):
    import time
    from datetime import datetime, timezone

    import fakeredis

    from scripts.swarm.health import merge_stall

    now, elapsed = 1_800_000_000_000, [0.0]
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    old = {
        "tried": 0,
        "queue": 2,
        "merged": now - 2 * merge_stall.HOUR_MS,
        "at": now,
        "repo": "o/r",
        "failed": "Previous check (2 runs)",
    }
    store.redis.set(store.key("sw", "merge-stall"), json.dumps(old))
    monkeypatch.setattr(time, "monotonic", lambda: elapsed[0])
    stamp = datetime.fromtimestamp(old["merged"] / 1000, timezone.utc).isoformat()

    def run(command, **kwargs):
        wait = min(14, kwargs["timeout"])
        elapsed[0] += wait
        if wait < 14:
            raise subprocess.TimeoutExpired(command, wait)
        if command[1:3] == ["repo", "view"]:
            data = {"nameWithOwner": "o/r"}
        elif command[2] == "graphql":
            data = {
                "data": {
                    "repository": {
                        "mergeQueue": {"entries": {"totalCount": 2}},
                        "pullRequests": {
                            "nodes": [{"mergedAt": stamp, "updatedAt": stamp}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            }
        elif "/jobs?" in command[2]:
            data = {"jobs": [{"name": "Lint", "conclusion": "failure"}]}
        else:
            data = {"workflow_runs": [{"id": 1, "conclusion": "failure"}]}
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

    monkeypatch.setattr(subprocess, "run", run)
    found = merge_stall.findings(store, "sw", "/repo", now)

    assert elapsed[0] < 30, f"Merge observation held findings for {elapsed[0]} seconds"
    assert len(found) == 1 and found[0].kind == "merge stall"
    assert "Previous check (2 runs)" in " ".join(found[0].evidence)
