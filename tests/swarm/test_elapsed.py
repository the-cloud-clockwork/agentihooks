import pytest

from scripts.swarm import elapsed
from scripts.swarm.tick import IDLE_KILL_MS, IDLE_KILL_TICKS, IDLE_NUDGE_MS, IDLE_NUDGE_TICKS, agent_status, tick
from tests.swarm.test_tick import IDLER, FakeRuntime, tasks
from tests.swarm.test_tick import store as store

MINUTE = 60_000


def test_limits_keep_the_one_minute_pass_counts():
    assert (IDLE_NUDGE_MS, IDLE_KILL_MS) == (2 * MINUTE, 19 * MINUTE)
    assert (IDLE_NUDGE_TICKS, IDLE_KILL_TICKS) == (3, 20)


@pytest.mark.parametrize(
    ("ms", "count"), [(0, 1), (29_999, 1), (30_000, 2), (MINUTE, 2), (89_999, 2), (90_000, 3), (-MINUTE, 1)]
)
def test_passes_counts_one_minute_passes_with_half_a_pass_of_jitter(ms, count):
    assert elapsed.passes(ms) == count


@pytest.mark.parametrize(("ms", "hit"), [(89_999, False), (90_000, True), (2 * MINUTE, True)])
def test_reached_allows_half_a_pass_of_jitter(ms, hit):
    assert elapsed.reached(ms, 2 * MINUTE) is hit


def test_anchored_turns_a_stored_count_into_its_start():
    assert elapsed.anchored(10 * MINUTE, 3) == 7 * MINUTE
    assert elapsed.anchored(10 * MINUTE, -1) == 10 * MINUTE


def test_start_keeps_a_running_event_and_restarts_a_new_one(store):
    assert elapsed.start(store, "sw", elapsed.IDLE_SINCE, IDLER, 5 * MINUTE, 0) == 5 * MINUTE
    assert elapsed.start(store, "sw", elapsed.IDLE_SINCE, IDLER, 9 * MINUTE, 4) == 5 * MINUTE
    assert elapsed.start(store, "sw", elapsed.IDLE_SINCE, IDLER, 12 * MINUTE, 0) == 12 * MINUTE
    assert store.redis.ttl(store.key("sw", elapsed.IDLE_SINCE, IDLER)) == elapsed.DAY_S


def test_start_anchors_a_count_older_code_kept_without_a_start(store):
    assert elapsed.start(store, "sw", elapsed.IDLE_SINCE, IDLER, 10 * MINUTE, 3) == 7 * MINUTE
    assert elapsed.read(store, "sw", elapsed.IDLE_SINCE, IDLER) == 7 * MINUTE


def test_hold_drops_the_start_so_the_next_pass_anchors_to_the_count(store):
    elapsed.start(store, "sw", elapsed.IDLE_SINCE, IDLER, 5 * MINUTE, 0)
    elapsed.hold(store, "sw", elapsed.IDLE_SINCE, IDLER, 0)
    assert elapsed.read(store, "sw", elapsed.IDLE_SINCE, IDLER) == 5 * MINUTE
    elapsed.hold(store, "sw", elapsed.IDLE_SINCE, IDLER, 2)
    assert elapsed.read(store, "sw", elapsed.IDLE_SINCE, IDLER) is None
    since = elapsed.start(store, "sw", elapsed.IDLE_SINCE, IDLER, 40 * MINUTE, 2)
    assert elapsed.passes(40 * MINUTE - since) == 3


def test_due_reads_the_stored_start_or_anchors_the_count(store):
    assert not elapsed.due(store, "sw", elapsed.IDLE_SINCE, IDLER, 30 * MINUTE, 0, MINUTE)
    assert elapsed.due(store, "sw", elapsed.IDLE_SINCE, IDLER, 30 * MINUTE, 19, IDLE_KILL_MS)
    assert not elapsed.due(store, "sw", elapsed.IDLE_SINCE, IDLER, 30 * MINUTE, 18, IDLE_KILL_MS)
    elapsed.start(store, "sw", elapsed.IDLE_SINCE, IDLER, 20 * MINUTE, 0)
    assert not elapsed.due(store, "sw", elapsed.IDLE_SINCE, IDLER, 30 * MINUTE, 19, IDLE_KILL_MS)
    assert elapsed.due(store, "sw", elapsed.IDLE_SINCE, IDLER, 39 * MINUTE, 1, IDLE_KILL_MS)


def stalled_and_retired(store, period_ms):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.statuses[IDLER] = "idle"
    store.update("sw", state="paused")
    stalled = None
    for n in range(30):
        at = n * period_ms
        tick("sw", store, ledger, runtime, now_ms=2_000 + at)
        if runtime.killed:
            assert ledger.rows["t1"]["state"] == "open"
            return stalled, at
        if stalled is None and agent_status(store.agents("sw")[0]) == "stalled":
            stalled = at
    raise AssertionError("the idle agent was never retired")


def test_a_one_minute_pass_marks_stalled_at_two_minutes_and_retires_at_nineteen(store):
    assert stalled_and_retired(store, MINUTE) == (2 * MINUTE, 19 * MINUTE)


def test_a_three_minute_pass_acts_at_its_first_pass_at_or_after_the_same_wall_time(store):
    slow = 3 * MINUTE
    found = stalled_and_retired(store, slow)
    assert found == tuple(-(-at // slow) * slow for at in (2 * MINUTE, 19 * MINUTE)) == (3 * MINUTE, 21 * MINUTE)
