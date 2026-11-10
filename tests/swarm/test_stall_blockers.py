import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hooks.context import blocker_window, master_coding
from scripts.gates import log as gate_log
from scripts.swarm import blockers
from scripts.swarm.health import panic_call, stall_blockers
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

SID = "master-session"
SLUG = "sw"
MINUTE = 60_000
HOUR = 60 * MINUTE


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> RedisStore:
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    found = RedisStore(redis)
    found.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=1))
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda environ=None: redis)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    master_coding.record_seat(SID, SLUG, "master@sw")
    return found


def _enforce(store: RedisStore) -> None:
    store.update(SLUG, gates={"stall-blockers": "enforce"})


def _now(store: RedisStore) -> int:
    return blocker_window._now_ms(store)


def _merged(store: RedisStore, at: int, tried: int | None = None) -> None:
    state = {"repo": "owner/repo", "queue": 0, "merged": at, "tried": _now(store) if tried is None else tried}
    store.redis.set(store.key(SLUG, "merge-stall"), json.dumps(state))


def _pulls(**states: PullRequest | None) -> Callable[[str], PullRequest | None]:
    return lambda url: states.get(url)


def _open() -> PullRequest:
    return PullRequest("OPEN", None, None, False)


def _done(at: int) -> PullRequest:
    return PullRequest("MERGED", at, None, False)


def _task(task_id: str, url: str) -> dict[str, Any]:
    return {"id": task_id, "title": f"task {task_id}", "state": "pr", "pr_url": url}


def _never(task: dict[str, Any]) -> bool:
    return False


def _hold(
    store: RedisStore, tasks: list[dict[str, Any]], github: Callable[[str], PullRequest | None], start: int, end: int
) -> None:
    for at in [*range(start, end, stall_blockers.STALE_MS), end]:
        _observe(store, tasks, github, at)


def _observe(
    store: RedisStore,
    tasks: list[dict[str, Any]],
    github: Callable[[str], PullRequest | None],
    now: int,
    held: Callable[[dict[str, Any]], bool] = _never,
) -> list[str]:
    return stall_blockers.observe(store, SLUG, {"tasks": tasks}, tasks, github, now, held)


@pytest.mark.feature("stall-blockers-open-master-coding")
def test_an_hour_without_a_dev_merge_while_a_pull_request_is_open_opens_the_master_coding_window(
    store: RedisStore,
) -> None:
    _enforce(store)
    now = _now(store)
    _merged(store, now - 2 * HOUR)
    github = _pulls(**{"pr-a": _open()})
    _hold(store, [_task("a", "pr-a")], github, now - HOUR, now - stall_blockers.STALE_MS)
    assert _observe(store, [_task("a", "pr-a")], github, now) == []
    found = blockers.sustained(store, SLUG, now)
    assert list(found) == [stall_blockers.MERGE_STALL]
    assert found[stall_blockers.MERGE_STALL].first_seen == now - HOUR
    assert (
        found[stall_blockers.MERGE_STALL].detail == "1 focused pull requests open and no merge into dev on owner/repo"
    )
    assert not master_coding.is_open(SID)
    blocker_window.pretool("Read", {}, SID)
    assert master_coding.is_open(SID)
    assert master_coding.opened_for_blockers(SID)


@pytest.mark.feature("stall-blockers-open-master-coding")
def test_a_task_left_in_pr_eighty_minutes_after_its_merge_opens_the_master_coding_window(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _done(now - 80 * MINUTE)}), now)
    found = blockers.sustained(store, SLUG, now)
    assert list(found) == [stall_blockers.MERGED_IN_PR]
    assert found[stall_blockers.MERGED_IN_PR].first_seen == now - HOUR
    assert (
        found[stall_blockers.MERGED_IN_PR].detail
        == "1 tasks still in pr after their pull request merged, oldest task a"
    )
    blocker_window.pretool("Read", {}, SID)
    assert master_coding.is_open(SID)


def test_short_of_the_hour_neither_category_is_sustained(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _merged(store, now - HOUR + 1)
    tasks = [_task("a", "pr-a"), _task("b", "pr-b")]
    _observe(store, tasks, _pulls(**{"pr-a": _open(), "pr-b": _done(now - 80 * MINUTE + 1)}), now)
    assert set(blockers.active(store, SLUG, now)) == set(stall_blockers.CATEGORIES)
    assert blockers.sustained(store, SLUG, now) == {}
    blocker_window.pretool("Read", {}, SID)
    assert not master_coding.is_open(SID)


def test_a_merge_nineteen_minutes_ago_or_a_held_task_records_nothing(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    tasks = [_task("a", "pr-a"), _task("b", "pr-b")]
    github = _pulls(**{"pr-a": _done(now - 19 * MINUTE), "pr-b": _done(now - 2 * HOUR)})
    assert _observe(store, tasks, github, now, held=lambda task: task["id"] == "b") == []
    assert blockers.active(store, SLUG, now) == {}


def test_the_oldest_merged_task_starts_the_category(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    tasks = [_task("a", "pr-a"), _task("b", "pr-b")]
    _observe(store, tasks, _pulls(**{"pr-a": _done(now - 30 * MINUTE), "pr-b": _done(now - 50 * MINUTE)}), now)
    found = blockers.active(store, SLUG, now)[stall_blockers.MERGED_IN_PR]
    assert found.first_seen == now - 30 * MINUTE
    assert found.detail == "2 tasks still in pr after their pull request merged, oldest task b"


def test_a_phase_focus_counts_only_the_pull_requests_of_its_phase(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _merged(store, now - 2 * HOUR)
    tasks = [{**_task("a", "pr-a"), "phase": "p1"}, {**_task("b", "pr-b"), "phase": "p2"}]
    doc = {
        "phases": [{"id": "p1"}, {"id": "p2"}],
        "tasks": tasks,
        "freezes": [{"verb": "focus", "target": "phases/p1"}],
    }
    stall_blockers.observe(store, SLUG, doc, tasks, _pulls(**{"pr-b": _open()}), now, _never)
    assert blockers.active(store, SLUG, now) == {}
    stall_blockers.observe(store, SLUG, doc, tasks, _pulls(**{"pr-a": _open()}), now, _never)
    assert list(blockers.active(store, SLUG, now)) == [stall_blockers.MERGE_STALL]


def test_an_open_pull_request_carrying_a_merge_time_is_not_merged(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    reopened = PullRequest("OPEN", now - 2 * HOUR, None, False)
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": reopened}), now)
    assert blockers.active(store, SLUG, now) == {}


def test_a_task_merged_exactly_twenty_minutes_ago_starts_the_category_now(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _done(now - stall_blockers.MERGED_MS)}), now)
    assert blockers.active(store, SLUG, now)[stall_blockers.MERGED_IN_PR].first_seen == now


def test_a_task_out_of_pr_listed_first_leaves_later_merged_tasks_counted(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    tasks = [{"id": "b", "state": "done", "pr_url": "pr-b"}, _task("a", "pr-a")]
    _observe(store, tasks, _pulls(**{"pr-a": _done(now - 2 * HOUR), "pr-b": _done(now - 2 * HOUR)}), now)
    found = blockers.active(store, SLUG, now)[stall_blockers.MERGED_IN_PR]
    assert found.detail == "1 tasks still in pr after their pull request merged, oldest task a"


def test_a_held_category_keeps_counting_while_its_start_holds(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    github = _pulls(**{"pr-a": _done(now - 2 * HOUR)})
    _observe(store, [_task("a", "pr-a")], github, now - MINUTE)
    _observe(store, [_task("a", "pr-a")], github, now)
    found = blockers.active(store, SLUG, now)[stall_blockers.MERGED_IN_PR]
    assert (found.first_seen, found.last_seen, found.count) == (now - 2 * HOUR + stall_blockers.MERGED_MS, now, 2)


def test_the_ledger_event_pass_records_the_merge_stall(store: RedisStore) -> None:
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events

    _enforce(store)
    now = _now(store)
    _merged(store, now - 2 * HOUR)
    doc = {"tasks": [_task("a", "pr-a")], "_meta": {"rev": 1, "events": []}}
    ledger_events.event_pass(InboxStore(store.redis), store, SLUG, doc, None, now, _pulls(**{"pr-a": _open()}))
    assert list(blockers.active(store, SLUG, now)) == [stall_blockers.MERGE_STALL]


def test_log_only_logs_a_merged_task_left_in_pr_once_and_keeps_state_expiring(store: RedisStore) -> None:
    now = _now(store)
    github = _pulls(**{"pr-a": _done(now - 2 * HOUR)})
    _observe(store, [_task("a", "pr-a")], github, now)
    _observe(store, [_task("a", "pr-a")], github, now + MINUTE)
    rows = [json.loads(line) for line in gate_log.gate_log_path(SLUG).read_text().splitlines()]
    reason = f"{stall_blockers.MERGED_IN_PR}: 1 tasks still in pr after their pull request merged, oldest task a"
    assert [row["reason"] for row in rows] == [reason]
    assert 0 < store.redis.pttl(store.key(SLUG, stall_blockers.KEY)) <= blockers.TTL_MS


def test_a_done_task_with_a_merged_pull_request_records_nothing(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    tasks = [_task("a", "pr-a"), {"id": "b", "state": "done", "pr_url": "pr-b"}]
    _observe(store, tasks, _pulls(**{"pr-a": _open(), "pr-b": _done(now - 2 * HOUR)}), now)
    assert blockers.active(store, SLUG, now) == {}


def test_a_merged_task_without_a_title_is_named_by_its_id(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    task = {"id": "t9", "state": "pr", "pr_url": "pr-a"}
    _observe(store, [task], _pulls(**{"pr-a": _done(now - 30 * MINUTE)}), now)
    found = blockers.active(store, SLUG, now)[stall_blockers.MERGED_IN_PR]
    assert found.detail == "1 tasks still in pr after their pull request merged, oldest t9"


def test_no_known_merge_or_no_open_pull_request_records_no_stall(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now)
    assert blockers.active(store, SLUG, now) == {}
    _merged(store, now - 2 * HOUR)
    _observe(store, [_task("a", "pr-a"), {"id": "b", "state": "open"}], _pulls(**{"pr-a": None}), now)
    assert blockers.active(store, SLUG, now) == {}


def test_a_stale_merge_observation_records_no_stall(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _merged(store, now - 2 * HOUR, tried=now - stall_blockers.STALE_MS - 1)
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now)
    assert blockers.active(store, SLUG, now) == {}
    _merged(store, now - 2 * HOUR, tried=now - stall_blockers.STALE_MS)
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now)
    assert list(blockers.active(store, SLUG, now)) == [stall_blockers.MERGE_STALL]


def test_state_left_unobserved_past_the_stale_window_starts_over(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _merged(store, now - 3 * HOUR)
    github = _pulls(**{"pr-a": _open()})
    _observe(store, [_task("a", "pr-a")], github, now - 2 * HOUR)
    _observe(store, [_task("a", "pr-a")], github, now - 2 * HOUR + stall_blockers.STALE_MS)
    assert blockers.active(store, SLUG, now)[stall_blockers.MERGE_STALL].first_seen == now - 2 * HOUR
    _observe(store, [_task("a", "pr-a")], github, now)
    assert blockers.active(store, SLUG, now)[stall_blockers.MERGE_STALL].first_seen == now


def test_a_ledger_without_pull_requests_leaves_its_categories_to_lapse(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _done(now - 2 * HOUR)}), now)
    assert list(blockers.sustained(store, SLUG, now)) == [stall_blockers.MERGED_IN_PR]
    assert _observe(store, [{"id": "a", "state": "done"}], _pulls(), now + blockers.RECENT_MS + 1) == []
    assert blockers.sustained(store, SLUG, now + blockers.RECENT_MS + 1) == {}


def test_a_ledger_without_pull_requests_reads_no_swarm_config(store: RedisStore) -> None:
    assert stall_blockers.observe(store, "no-such-swarm", {}, [{"id": "a", "state": "open"}], _pulls(), 0, _never) == []


def test_a_merge_observation_error_records_no_stall(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    state = {"repo": "owner/repo", "merged": now - 2 * HOUR, "error": "rate limited"}
    store.redis.set(store.key(SLUG, "merge-stall"), json.dumps(state))
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now)
    assert blockers.active(store, SLUG, now) == {}


def test_a_focus_counts_only_its_own_pull_requests(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _merged(store, now - 2 * HOUR)
    tasks = [_task("a", "pr-a"), _task("b", "pr-b")]
    doc = {"tasks": tasks, "freezes": [{"verb": "focus", "target": "tasks/b"}]}
    stall_blockers.observe(store, SLUG, doc, tasks, _pulls(**{"pr-a": _open(), "pr-b": _merged_pr(now)}), now, _never)
    assert blockers.active(store, SLUG, now) == {}
    stall_blockers.observe(store, SLUG, doc, tasks, _pulls(**{"pr-a": _open(), "pr-b": _open()}), now, _never)
    assert list(blockers.active(store, SLUG, now)) == [stall_blockers.MERGE_STALL]


def _merged_pr(now: int) -> PullRequest:
    return _done(now - MINUTE)


def test_a_new_dev_merge_restarts_the_stall_and_a_resolved_condition_clears_it(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _merged(store, now - 2 * HOUR)
    github = _pulls(**{"pr-a": _open()})
    _hold(store, [_task("a", "pr-a")], github, now - HOUR, now - stall_blockers.STALE_MS)
    _merged(store, now - 10 * MINUTE)
    _observe(store, [_task("a", "pr-a")], github, now)
    assert blockers.active(store, SLUG, now)[stall_blockers.MERGE_STALL].first_seen == now - 10 * MINUTE
    assert blockers.sustained(store, SLUG, now) == {}
    assert _observe(store, [_task("a", "pr-a")], _pulls(), now) == [
        f"stall blockers: cleared {stall_blockers.MERGE_STALL}"
    ]
    assert blockers.active(store, SLUG, now) == {}
    assert _observe(store, [_task("a", "pr-a")], _pulls(), now) == []


def test_a_stall_older_than_the_queue_lifetime_stays_sustained(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _merged(store, now - 30 * HOUR)
    store.redis.set(store.key(SLUG, stall_blockers.KEY), json.dumps({"open_since": now - 29 * HOUR}))
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now)
    found = blockers.sustained(store, SLUG, now)[stall_blockers.MERGE_STALL]
    assert found.first_seen == now - blockers.TTL_MS // 2


@pytest.mark.feature("stall-blockers-open-master-coding")
def test_log_only_by_default_writes_the_gate_log_and_records_no_blocker(store: RedisStore) -> None:
    now = _now(store)
    _merged(store, now - HOUR)
    assert _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now) == []
    reason = f"{stall_blockers.MERGE_STALL}: 1 focused pull requests open and no merge into dev on owner/repo"
    assert blockers.active(store, SLUG, now) == {}

    def logged() -> list[tuple[str, str, str, str, int]]:
        rows = [json.loads(line) for line in gate_log.gate_log_path(SLUG).read_text().splitlines()]
        return [(row["gate"], row["kind"], row["agent"], row["reason"], row["at"]) for row in rows]

    assert logged() == [("stall-blockers", "observe", "swarm", reason, now)]
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now + MINUTE)
    assert len(logged()) == 1
    _observe(store, [_task("a", "pr-a")], _pulls(), now)
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now + 2 * MINUTE)
    assert logged()[1:] == [("stall-blockers", "observe", "swarm", reason, now + 2 * MINUTE)]


def test_a_pull_request_opened_long_after_the_last_merge_waits_its_own_hour(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _merged(store, now - 3 * HOUR)
    github = _pulls(**{"pr-a": _open()})
    _hold(store, [_task("a", "pr-a")], github, now - HOUR + 1, now)
    assert blockers.active(store, SLUG, now)[stall_blockers.MERGE_STALL].first_seen == now - HOUR + 1
    assert blockers.sustained(store, SLUG, now) == {}
    _observe(store, [_task("a", "pr-a")], _pulls(), now)
    _observe(store, [_task("a", "pr-a")], github, now + MINUTE)
    assert blockers.active(store, SLUG, now + MINUTE)[stall_blockers.MERGE_STALL].first_seen == now + MINUTE


def test_off_skips_both_categories(store: RedisStore) -> None:
    store.update(SLUG, gates={"stall-blockers": "off"})
    now = _now(store)
    _merged(store, now - 2 * HOUR)
    assert _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now) == []
    assert blockers.active(store, SLUG, now) == {}
    assert not gate_log.gate_log_path(SLUG).exists()


def test_a_recorded_since_starts_only_a_first_sighting() -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    blockers.record(store, SLUG, "cat", "one", 100, since=40)
    blockers.record(store, SLUG, "cat", "two", 200, since=150)
    assert blockers.active(store, SLUG, 200)["cat"] == blockers.Blocker("cat", 40, 200, 2, "two")
    blockers.record(store, SLUG, "other", "one", 300)
    assert blockers.active(store, SLUG, 300)["other"].first_seen == 300


def test_panic_call_reads_no_stall_category_as_a_tick_step_failure(store: RedisStore) -> None:
    _enforce(store)
    now = _now(store)
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _done(now - 2 * HOUR)}), now)
    assert panic_call._steps(store, SLUG, now) == []
