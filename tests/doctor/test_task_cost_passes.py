from copy import deepcopy
from types import SimpleNamespace

import fakeredis
import pytest

from scripts.doctor import detect
from scripts.swarm.store import RedisStore


def _trace(task, tokens, end):
    return {
        "id": task,
        "name": task,
        "session_id": task,
        "tags": [f"task:{task}"],
        "complete": True,
        "observations": [
            {"type": "GENERATION", "turn": None, "tokens": tokens},
            {"type": "SPAN", "turn": 1, "start": end - 1, "end": end},
        ],
    }


@pytest.fixture
def scan(monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    tasks = [{"id": task, "state": "done", "pr_url": f"https://example.com/{task}"} for task in ("a", "b", "old")]
    record = {
        "slug": "watched",
        "now_ms": 1000,
        "merged": ["a", "b", "old"],
        "traces": [_trace("a", 100, 10), _trace("b", 100, 20), _trace("old", 1000, 30)],
        "sessions": [],
        "active": [],
        "reader": {
            "failures": [],
            "down_since": 0,
            "active": {"bindings": 0, "read": 0},
            "historical": {"listed": True, "complete": True, "traces": 3, "covered": 3},
        },
    }
    monkeypatch.setattr(detect.traces_read, "record", lambda *args: deepcopy(record))

    def run():
        store = RedisStore(redis)
        ledger = SimpleNamespace(tasks=lambda slug: tasks)
        return [
            f
            for f in detect.readers(store, ledger, "watched", record["now_ms"], environ={})["trace"]()
            if f.kind == "task cost"
        ]

    return run, tasks, record


def _merge(tasks, record, task="new", tokens=2000):
    tasks.append({"id": task, "state": "done", "pr_url": f"https://example.com/{task}"})
    record["merged"].append(task)
    record["traces"].append(_trace(task, tokens, 40))
    record["now_ms"] += 1000


def test_old_backlog_is_baselined_and_new_merges_are_judged_once_across_readers(scan):
    run, tasks, record = scan
    assert run() == []
    _merge(tasks, record)
    [finding] = run()
    assert finding.subject == "new"
    assert "median of the other merged tasks 100" in finding.evidence[2]
    assert run() == []


@pytest.mark.parametrize("gap", ["failure", "listing", "observations", "capped"])
def test_incomplete_passes_preserve_new_merges_until_recovery(scan, gap):
    run, tasks, record = scan
    assert run() == []
    _merge(tasks, record)
    if gap == "failure":
        record["reader"]["failures"] = ["trace read timed out"]
    elif gap == "listing":
        record["reader"]["historical"]["listed"] = False
    elif gap == "observations":
        record["reader"]["historical"]["complete"] = False
    else:
        record["traces"][-1]["complete"] = False
        record["reader"]["historical"]["capped"] = 1
    assert run() == []
    record["reader"]["failures"] = []
    record["reader"]["historical"] = {"listed": True, "complete": True, "traces": 4, "covered": 4}
    record["traces"][-1]["complete"] = True
    assert [f.subject for f in run()] == ["new"]
    assert run() == []


def test_backlog_first_seen_during_outage_is_not_new_on_recovery(scan):
    run, tasks, record = scan
    record["reader"]["failures"] = ["trace read timed out"]
    assert run() == []
    _merge(tasks, record)
    record["reader"]["failures"] = []
    assert [f.subject for f in run()] == ["new"]
    assert run() == []

@pytest.mark.parametrize("missing", ["trace", "turns"])
def test_a_new_merge_waits_for_its_exported_cost_before_it_is_consumed(scan, missing):
    run, tasks, record = scan
    assert run() == []
    _merge(tasks, record)
    exported = deepcopy(record["traces"][-1])
    if missing == "trace":
        record["traces"].pop()
    else:
        record["traces"][-1]["observations"] = []
    assert run() == []
    if missing == "trace":
        record["traces"].append(exported)
    else:
        record["traces"][-1] = exported
    assert [f.subject for f in run()] == ["new"]
    assert run() == []
