import subprocess
from collections.abc import Callable

import pytest

from scripts.swarm.health import checks
from scripts.swarm.health.findings import Limits
from scripts.swarm.keyspace import ROOT as KEY_ROOT
from scripts.tick.ports.source_control import CheckState

URL = "https://github.com/o/r/pull/7"
PASS = CheckState("lint", "completed", "success", "https://x/1")
WAIT = CheckState("unit (3.11, 1)", "in_progress", None, "https://x/2")
FAIL = CheckState("lint", "completed", "failure", "https://x/1")
SKIP = CheckState("refresh-durations", "completed", "skipped", "https://x/3")
NEUTRAL = CheckState("advisory", "completed", "neutral", "https://x/4")
PENDING = (PASS, WAIT)
PASSED = (PASS, CheckState("unit (3.11, 1)", "completed", "success", "https://x/2"))
SKIPPED = (*PASSED, SKIP)


def runner(found: tuple[CheckState, ...], seen: list[str] | None = None) -> Callable[[str], tuple[CheckState, ...]]:
    def read(pr_url: str) -> tuple[CheckState, ...]:
        if seen is not None:
            seen.append(pr_url)
        return found

    return read


def raising(pr_url: str) -> tuple[CheckState, ...]:
    raise subprocess.TimeoutExpired(["gh"], 20)


def test_a_pull_request_with_a_pending_check_waits_on_checks():
    seen = []
    assert checks.pending(URL, runner(PENDING, seen)) is True
    assert seen == [URL]


def test_finished_failed_or_unreadable_checks_do_not_wait():
    assert checks.pending(URL, runner(PASSED)) is False
    assert checks.pending(URL, runner((FAIL,))) is False
    assert checks.pending(URL, raising) is False


def test_approval_waits_only_when_every_check_passed():
    assert checks.pending(URL, runner(PASSED), approval=True) is True
    assert checks.pending(URL, runner(SKIPPED), approval=True) is False
    assert checks.pending(URL, runner(()), approval=True) is False


def test_check_runs_are_named_by_status_and_conclusion():
    assert checks.passing(URL, runner((PASS, NEUTRAL))) is True
    assert checks.passing(URL, runner((PASS, CheckState("x", "queued", None, "u")))) is False
    assert checks.passing(URL, runner((PASS, CheckState("x", "completed", None, "u")))) is False
    assert checks.SKIPPED == ("skipped", "neutral")


def test_read_checks_asks_the_port_for_the_pull_request_head(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import stack
    from scripts.tick.adapters.fakes import FakeSourceControl
    from scripts.tick.ports.source_control import ChangeRef

    reference = ChangeRef("o/r", 7)
    provider = FakeSourceControl(answers={"checks": PENDING})
    monkeypatch.setattr(stack, "source_control", lambda url: (provider, reference) if url == URL else None)
    assert checks.read_checks(URL) == PENDING
    [call] = provider.calls
    assert (call.operation, call.args) == ("checks", (reference,))
    assert stack.PORT_SECONDS - 1 < call.deadline.remaining <= stack.PORT_SECONDS


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
    probe = checks.cached(redis, f"{KEY_ROOT}:swarm:sw:checks", read=runner(PENDING, seen))
    assert [probe(URL), probe(URL)] == [True, True]
    assert len(seen) == 1
    assert 0 < redis.ttl(f"{KEY_ROOT}:swarm:sw:checks:{URL}") <= checks.CACHE_SECONDS


def test_a_pull_request_whose_checks_all_passed_or_skipped_is_green():
    seen = []
    assert checks.passing(URL, runner(SKIPPED, seen)) is True
    assert seen == [URL]


def test_pending_failed_empty_or_unreadable_checks_are_not_green():
    assert checks.passing(URL, runner(PENDING)) is False
    assert checks.passing(URL, runner((*PASSED, FAIL))) is False
    assert checks.passing(URL, runner((FAIL,))) is False
    assert checks.passing(URL, runner((SKIP,))) is False
    assert checks.passing(URL, runner(())) is False
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
    probe = checks.cached_green(redis, f"{KEY_ROOT}:swarm:sw:checks", read=runner(PASSED, seen))
    assert [probe(URL), probe(URL)] == [True, True]
    assert len(seen) == 1
    assert redis.get(f"{KEY_ROOT}:swarm:sw:checks:green:{URL}") == "1"
    assert 0 < redis.ttl(f"{KEY_ROOT}:swarm:sw:checks:green:{URL}") <= checks.CACHE_SECONDS
    red = checks.cached_green(redis, f"{KEY_ROOT}:swarm:sw:checks", read=runner(PENDING))
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


@pytest.mark.parametrize(
    "pending,passed,approval,expected",
    [
        (None, None, False, (False, False)),
        (True, False, False, (True, False)),
        (False, True, False, (False, True)),
        (False, True, True, (True, True)),
        (False, False, True, (False, False)),
    ],
)
def test_snapshot_check_probes_preserve_unknown_waiting_and_approval_states(pending, passed, approval, expected):
    from types import SimpleNamespace

    from scripts.swarm.health import checks

    snapshot = None if pending is None else SimpleNamespace(checks_pending=pending, checks_passed=passed)

    def view(url):
        assert url == URL
        return snapshot

    waiting, green = checks.from_view(view, approval=approval)
    assert (waiting(URL), green(URL)) == expected


def test_default_snapshot_probe_treats_passing_checks_as_finished():
    from types import SimpleNamespace

    waiting, green = checks.from_view(lambda url: SimpleNamespace(checks_pending=False, checks_passed=True))
    assert waiting(URL) is False
    assert green(URL) is True
