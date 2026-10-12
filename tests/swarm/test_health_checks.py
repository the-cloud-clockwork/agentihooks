import subprocess

import pytest

from scripts.swarm.health import checks
from scripts.swarm.health.findings import Limits
from scripts.swarm.keyspace import ROOT as KEY_ROOT

pytestmark = pytest.mark.xdist_group("fakeredis")

URL = "https://github.com/o/r/pull/7"
PENDING = "lint\tpass\t8s\thttps://x/1\nunit (3.11, 1)\tpending\t0\thttps://x/2\n"
PASSED = "lint\tpass\t8s\thttps://x/1\nunit (3.11, 1)\tpass\t12s\thttps://x/2\n"
SKIPPED = PASSED + "refresh-durations\tskipping\t0\thttps://x/3\n"


def runner(out, code=0, seen=None):
    def run(argv, **_):
        if seen is not None:
            seen.append(argv)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    return run


def raising(argv, **_):
    raise subprocess.TimeoutExpired(argv, 20)


def test_a_pull_request_with_a_pending_check_waits_on_checks():
    seen = []
    assert checks.pending(URL, runner(PENDING, 8, seen)) is True
    assert seen == [["gh", "pr", "checks", URL]]


def test_finished_failed_or_unreadable_checks_do_not_wait():
    assert checks.pending(URL, runner(PASSED)) is False
    assert checks.pending(URL, runner("lint\tfail\t8s\thttps://x/1\n", 1)) is False
    assert checks.pending(URL, raising) is False


def test_only_idle_holders_of_a_pull_request_task_are_probed():
    tasks = [
        {"id": "t1", "state": "pr", "pr_url": URL},
        {"id": "t2", "state": "pr", "pr_url": URL},
        {"id": "t3", "state": "claimed", "pr_url": ""},
    ]
    agents = [
        {"name": "sw-eng-1", "lane": "eng", "task": "t1", "idle_ticks": 4},
        {"name": "sw-eng-2", "lane": "eng", "task": "t2", "idle_ticks": 1},
        {"name": "sw-eng-3", "lane": "eng", "task": "t3", "idle_ticks": 9},
    ]
    seen = []
    assert checks.waiting(agents, tasks, Limits(), lambda url: seen.append(url) or True) == {"t1"}
    assert seen == [URL]


def test_a_cached_probe_asks_github_once_per_ttl():
    import fakeredis

    redis, seen = fakeredis.FakeRedis(decode_responses=True), []
    probe = checks.cached(redis, f"{KEY_ROOT}:swarm:sw:checks", run=runner(PENDING, 8, seen))
    assert [probe(URL), probe(URL)] == [True, True]
    assert len(seen) == 1
    assert 0 < redis.ttl(f"{KEY_ROOT}:swarm:sw:checks:{URL}") <= checks.CACHE_SECONDS


def test_a_pull_request_whose_checks_all_passed_or_skipped_is_green():
    seen = []
    assert checks.passing(URL, runner(SKIPPED, 0, seen)) is True
    assert seen == [["gh", "pr", "checks", URL]]


def test_pending_failed_empty_or_unreadable_checks_are_not_green():
    assert checks.passing(URL, runner(PENDING, 8)) is False
    assert checks.passing(URL, runner(PASSED, 1)) is False
    assert checks.passing(URL, runner("lint\tfail\t8s\thttps://x/1\n", 1)) is False
    assert checks.passing(URL, runner("refresh-durations\tskipping\t0\thttps://x/3\n")) is False
    assert checks.passing(URL, runner("", 1)) is False
    assert checks.passing(URL, raising) is False


def test_only_tasks_in_pull_request_state_with_a_link_are_probed_for_green():
    tasks = [
        {"id": "t1", "state": "pr", "pr_url": URL},
        {"id": "t2", "state": "pr", "pr_url": ""},
        {"id": "t3", "state": "done", "pr_url": URL},
        {"id": "t4", "state": "pr", "pr_url": "https://github.com/o/r/pull/8"},
    ]
    seen = []
    assert checks.green(tasks, lambda url: seen.append(url) or url == URL) == {"t1"}
    assert seen == [URL, "https://github.com/o/r/pull/8"]


def test_a_cached_green_probe_asks_github_once_per_ttl_under_its_own_key():
    import fakeredis

    redis, seen = fakeredis.FakeRedis(decode_responses=True), []
    probe = checks.cached_green(redis, f"{KEY_ROOT}:swarm:sw:checks", run=runner(PASSED, 0, seen))
    assert [probe(URL), probe(URL)] == [True, True]
    assert len(seen) == 1
    assert redis.get(f"{KEY_ROOT}:swarm:sw:checks:green:{URL}") == "1"
    assert 0 < redis.ttl(f"{KEY_ROOT}:swarm:sw:checks:green:{URL}") <= checks.CACHE_SECONDS
    red = checks.cached_green(redis, f"{KEY_ROOT}:swarm:sw:checks", run=runner(PENDING, 8))
    assert [red("https://github.com/o/r/pull/8"), red("https://github.com/o/r/pull/8")] == [False, False]
    assert redis.get(f"{KEY_ROOT}:swarm:sw:checks:green:https://github.com/o/r/pull/8") == "0"


def test_status_raises_worker_ceremony_only_past_the_talk_budget(monkeypatch):
    import fakeredis

    from scripts.gates.progress import Progress
    from scripts.swarm import status
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("green-proof", "/repo", max_eng=1, max_ci=0)
    store.create(config)
    store.put_agent("green-proof", AgentRecord("sw-eng-1", "eng", "t1"))
    monkeypatch.setattr(status.activity, "counts", lambda slug: {})
    tasks = [{"id": "t1", "state": "pr", "claimed_by": "sw-eng-1", "pr_url": URL}]
    at = status.now_ms()
    events = [{"kind": "comment edited", "target": "phases/p1", "by": "sw-eng-1", "at": at} for _ in range(25)]
    store.redis.set(f"{store.key('green-proof', 'checks')}:green:{URL}", "0")
    marks = Progress(store.redis, "green-proof")
    marks.outcome("sw-eng-1", "pushed", at)
    for _ in range(10):
        marks.talk("sw-eng-1")
    assert status.findings(store, "green-proof", config, tasks, events) == []
    marks.talk("sw-eng-1")
    found = status.findings(store, "green-proof", config, tasks, events)
    assert [(f["kind"], f["subject"], f["evidence"]) for f in found] == [
        ("ceremony", "sw-eng-1", ["11 talk writes since its last outcome"])
    ]


@pytest.mark.parametrize(("decided", "expected"), [(True, []), (False, [["53 ledger transitions", "0 outcomes"]])])
def test_status_credits_the_dispatcher_seat_with_the_priorities_it_settled(monkeypatch, decided, expected):
    import fakeredis

    from scripts.swarm import status
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    seat = "dispatcher@323133-0001"
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("triage-proof", "/repo", max_eng=1, max_ci=0)
    store.create(config)
    store.put_agent("triage-proof", AgentRecord(seat, "dispatch", "dispatcher"))
    monkeypatch.setattr(status.activity, "counts", lambda slug: {})
    at = status.now_ms()
    events = []
    for n in range(24):
        kind, target = ("comment added", f"followups/f{n}") if decided else ("comment edited", "phases/p1")
        events.append({"kind": kind, "target": target, "by": seat, "at": at})
        events.append({"kind": "checked", "target": f"followups/f{n}", "by": seat, "at": at})
    events += [{"kind": "priority cleared", "target": f"followups/c{n}", "by": seat, "at": at} for n in range(5)]
    found = status.findings(store, "triage-proof", config, [], events)
    assert [f["evidence"] for f in found if f["kind"] == "ceremony"] == expected
