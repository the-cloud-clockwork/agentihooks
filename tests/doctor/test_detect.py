import pytest

from scripts.doctor import detect
from scripts.swarm.health.findings import Finding

pytestmark = pytest.mark.xdist_group("fakeredis")

STALE = Finding("stale claim", "watch-eng-1", "no change for 40 minutes", ("task t1",), "30 minutes", 40)


def test_a_failing_detector_is_named_and_the_others_still_report():
    def broken():
        raise RuntimeError("journal unreadable")

    found, failed = detect.collect({"spawn": broken, "health": lambda: [STALE]})
    assert found == [STALE]
    assert failed == ["the spawn detector failed: RuntimeError: journal unreadable"]


def test_ci_reads_only_the_pull_requests_of_tasks_waiting_in_review():
    tasks = [
        {"state": "pr", "pr_url": "https://github.com/o/r/pull/9"},
        {"state": "pr", "pr_url": "https://github.com/o/r/pull/9"},
        {"state": "done", "pr_url": "https://github.com/o/r/pull/3"},
        {"state": "pr", "pr_url": ""},
        {"state": "pr", "pr_url": "https://github.com/o/other/pull/12"},
    ]
    assert detect.open_pulls(tasks) == [("o/other", 12), ("o/r", 9)]


def test_the_health_detector_judges_only_what_the_current_health_pass_reports(monkeypatch):
    import json

    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    record = {"seen_at": 0, "verdict": None, "returned": False, "evidence": ["task t1 (pr)"], "measure": 4}
    for agent in ("s-eng-1", "s-eng-2"):
        store.redis.hset(store.key("s", "findings"), f"idle-with-claim/{agent}", json.dumps(record))
    states = {"s": {"tasks": [{"id": "t1"}], "_meta": {"events": [{"kind": "x"}]}}, "bare": {}}
    ledger = type("Ledger", (), {"state": lambda self, slug: states[slug]})()
    seen = []

    def current(store_, slug, config, tasks, events):
        seen.append((store_, slug, config, tasks, events))
        return [{"id": "idle-with-claim/s-eng-2"}]

    monkeypatch.setattr(detect.status, "findings", current)
    monkeypatch.setattr(store, "config", lambda slug: f"config of {slug}")
    found = detect.readers(store, ledger, "s", 10**12, environ={})["health"]()
    assert [f.id for f in found] == ["unjudged-finding/idle-with-claim/s-eng-2"]
    assert seen == [(store, "s", "config of s", states["s"]["tasks"], states["s"]["_meta"]["events"])]
    assert detect.reported(store, ledger, "bare") == {"idle-with-claim/s-eng-2"}
    assert seen[-1] == (store, "bare", "config of bare", [], [])


def test_the_trace_detector_reads_the_swarm_tag_and_its_sessions(monkeypatch):
    from scripts.doctor import traces_read
    from scripts.swarm.store import AgentRecord

    asked = []

    def get(path, params):
        asked.append((path, params))
        return {"data": [], "meta": {"page": 1, "totalPages": 1}}

    store = type("Store", (), {"redis": object()})()
    store.agents = lambda slug: [AgentRecord("s-master-1", "master", "master", conversation_id="c1", started_at=1)]
    ledger = type("Ledger", (), {"tasks": lambda self, slug: []})()
    monkeypatch.setattr(traces_read, "client", lambda env: get)
    found = detect.readers(store, ledger, "s", 10**12, environ={})["trace"]()
    assert asked == [("traces", {"tags": "swarm:s", "page": 1, "limit": 100})]
    assert [f.id for f in found] == ["untraced-session/s-master-1"]
