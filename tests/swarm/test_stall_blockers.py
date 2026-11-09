import json

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
def store(monkeypatch, tmp_path):
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    found = RedisStore(redis)
    found.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=1))
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda environ=None: redis)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    master_coding.record_seat(SID, SLUG, "master@sw")
    return found


def _enforce(store):
    store.update(SLUG, gates={"stall-blockers": "enforce"})


def _now(store):
    return blocker_window._now_ms(store)


def _merged(store, at):
    store.redis.set(store.key(SLUG, "merge-stall"), json.dumps({"repo": "owner/repo", "queue": 0, "merged": at}))


def _pulls(**states):
    return lambda url: states.get(url)


def _open():
    return PullRequest("OPEN", None, None, False)


def _done(at):
    return PullRequest("MERGED", at, None, False)


def _task(task_id, url):
    return {"id": task_id, "title": f"task {task_id}", "state": "pr", "pr_url": url}


def _observe(store, tasks, github, now, held=lambda task: False):
    return stall_blockers.observe(store, SLUG, {"tasks": tasks}, tasks, github, now, held)


@pytest.mark.feature("stall-blockers-open-master-coding")
def test_an_hour_without_a_dev_merge_while_a_pull_request_is_open_opens_the_master_coding_window(store):
    _enforce(store)
    now = _now(store)
    _merged(store, now - 2 * HOUR)
    github = _pulls(**{"pr-a": _open()})
    assert _observe(store, [_task("a", "pr-a")], github, now - HOUR) == []
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
def test_a_task_left_in_pr_eighty_minutes_after_its_merge_opens_the_master_coding_window(store):
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


def test_short_of_the_hour_neither_category_is_sustained(store):
    _enforce(store)
    now = _now(store)
    _merged(store, now - HOUR + 1)
    tasks = [_task("a", "pr-a"), _task("b", "pr-b")]
    _observe(store, tasks, _pulls(**{"pr-a": _open(), "pr-b": _done(now - 80 * MINUTE + 1)}), now)
    assert set(blockers.active(store, SLUG, now)) == set(stall_blockers.CATEGORIES)
    assert blockers.sustained(store, SLUG, now) == {}
    blocker_window.pretool("Read", {}, SID)
    assert not master_coding.is_open(SID)


def test_a_merge_nineteen_minutes_ago_or_a_held_task_records_nothing(store):
    _enforce(store)
    now = _now(store)
    tasks = [_task("a", "pr-a"), _task("b", "pr-b")]
    github = _pulls(**{"pr-a": _done(now - 19 * MINUTE), "pr-b": _done(now - 2 * HOUR)})
    assert _observe(store, tasks, github, now, held=lambda task: task["id"] == "b") == []
    assert blockers.active(store, SLUG, now) == {}


def test_the_oldest_merged_task_starts_the_category(store):
    _enforce(store)
    now = _now(store)
    tasks = [_task("a", "pr-a"), _task("b", "pr-b")]
    _observe(store, tasks, _pulls(**{"pr-a": _done(now - 30 * MINUTE), "pr-b": _done(now - 50 * MINUTE)}), now)
    found = blockers.active(store, SLUG, now)[stall_blockers.MERGED_IN_PR]
    assert found.first_seen == now - 30 * MINUTE
    assert found.detail == "2 tasks still in pr after their pull request merged, oldest task b"


def test_no_open_pull_request_or_no_known_merge_records_no_stall(store):
    _enforce(store)
    now = _now(store)
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now)
    _merged(store, now - 2 * HOUR)
    _observe(store, [_task("a", "pr-a"), {"id": "b", "state": "open"}], _pulls(**{"pr-a": None}), now)
    assert blockers.active(store, SLUG, now) == {}


def test_a_merge_observation_error_records_no_stall(store):
    _enforce(store)
    now = _now(store)
    state = {"repo": "owner/repo", "merged": now - 2 * HOUR, "error": "rate limited"}
    store.redis.set(store.key(SLUG, "merge-stall"), json.dumps(state))
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now)
    assert blockers.active(store, SLUG, now) == {}


def test_a_focus_counts_only_its_own_pull_requests(store):
    _enforce(store)
    now = _now(store)
    _merged(store, now - 2 * HOUR)
    tasks = [_task("a", "pr-a"), _task("b", "pr-b")]
    doc = {"tasks": tasks, "freezes": [{"verb": "focus", "target": "tasks/b"}]}
    stall_blockers.observe(store, SLUG, doc, tasks, _pulls(**{"pr-a": _open(), "pr-b": _merged_pr(now)}), now, bool)
    assert blockers.active(store, SLUG, now) == {}
    stall_blockers.observe(store, SLUG, doc, tasks, _pulls(**{"pr-a": _open(), "pr-b": _open()}), now, bool)
    assert list(blockers.active(store, SLUG, now)) == [stall_blockers.MERGE_STALL]


def _merged_pr(now):
    return _done(now - MINUTE)


def test_a_new_dev_merge_restarts_the_stall_and_a_resolved_condition_clears_it(store):
    _enforce(store)
    now = _now(store)
    _merged(store, now - 2 * HOUR)
    github = _pulls(**{"pr-a": _open()})
    _observe(store, [_task("a", "pr-a")], github, now - HOUR)
    _merged(store, now - 10 * MINUTE)
    _observe(store, [_task("a", "pr-a")], github, now)
    assert blockers.active(store, SLUG, now)[stall_blockers.MERGE_STALL].first_seen == now - 10 * MINUTE
    assert blockers.sustained(store, SLUG, now) == {}
    assert _observe(store, [], github, now) == [f"stall blockers: cleared {stall_blockers.MERGE_STALL}"]
    assert blockers.active(store, SLUG, now) == {}
    assert _observe(store, [], github, now) == []


def test_a_stall_older_than_the_queue_lifetime_stays_sustained(store):
    _enforce(store)
    now = _now(store)
    _merged(store, now - 30 * HOUR)
    store.redis.set(store.key(SLUG, stall_blockers.KEY), json.dumps({"open_since": now - 29 * HOUR}))
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now)
    found = blockers.sustained(store, SLUG, now)[stall_blockers.MERGE_STALL]
    assert found.first_seen == now - blockers.TTL_MS // 2


@pytest.mark.feature("stall-blockers-open-master-coding")
def test_log_only_by_default_writes_the_gate_log_and_records_no_blocker(store):
    now = _now(store)
    _merged(store, now - HOUR)
    lines = _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now)
    reason = f"{stall_blockers.MERGE_STALL}: 1 focused pull requests open and no merge into dev on owner/repo"
    assert lines == [f"stall blockers: logged {reason}"]
    assert blockers.active(store, SLUG, now) == {}
    rows = [json.loads(line) for line in gate_log.gate_log_path(SLUG).read_text().splitlines()]
    assert [(row["gate"], row["kind"], row["agent"], row["reason"], row["at"]) for row in rows] == [
        ("stall-blockers", "observe", "swarm", reason, now)
    ]
    assert _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now + MINUTE) == []
    assert len(gate_log.gate_log_path(SLUG).read_text().splitlines()) == 1
    assert _observe(store, [], _pulls(), now) == []
    assert _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now + 2 * MINUTE) == [
        f"stall blockers: logged {reason}"
    ]


def test_a_pull_request_opened_long_after_the_last_merge_waits_its_own_hour(store):
    _enforce(store)
    now = _now(store)
    _merged(store, now - 3 * HOUR)
    github = _pulls(**{"pr-a": _open()})
    _observe(store, [_task("a", "pr-a")], github, now - HOUR + 1)
    _observe(store, [_task("a", "pr-a")], github, now)
    assert blockers.active(store, SLUG, now)[stall_blockers.MERGE_STALL].first_seen == now - HOUR + 1
    assert blockers.sustained(store, SLUG, now) == {}
    _observe(store, [], github, now)
    _observe(store, [_task("a", "pr-a")], github, now + MINUTE)
    assert blockers.active(store, SLUG, now + MINUTE)[stall_blockers.MERGE_STALL].first_seen == now + MINUTE


def test_off_skips_both_categories(store):
    store.update(SLUG, gates={"stall-blockers": "off"})
    now = _now(store)
    _merged(store, now - 2 * HOUR)
    assert _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _open()}), now) == []
    assert blockers.active(store, SLUG, now) == {}
    assert not gate_log.gate_log_path(SLUG).exists()


def test_a_recorded_since_starts_only_a_first_sighting():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    blockers.record(store, SLUG, "cat", "one", 100, since=40)
    blockers.record(store, SLUG, "cat", "two", 200, since=150)
    assert blockers.active(store, SLUG, 200)["cat"] == blockers.Blocker("cat", 40, 200, 2, "two")
    blockers.record(store, SLUG, "other", "one", 300)
    assert blockers.active(store, SLUG, 300)["other"].first_seen == 300


def test_panic_call_reads_no_stall_category_as_a_tick_step_failure(store):
    _enforce(store)
    now = _now(store)
    _observe(store, [_task("a", "pr-a")], _pulls(**{"pr-a": _done(now - 2 * HOUR)}), now)
    assert panic_call._steps(store, SLUG, now) == []
