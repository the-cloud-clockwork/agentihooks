import threading
import time

import pytest

from scripts.swarm_ledger.events import hub as events_hub
from scripts.swarm_ledger.events.hub import Expired, Hub
from scripts.swarm_ledger.events.patch import apply
from tests.swarm_ledger.bounded import bounded

SLUG = "hub-2026-01-01"


def ledger(rev, **fields):
    return {"tasks": [], **fields, "_meta": {"rev": rev}}


def opened(hub, slug=SLUG, cursor=None, rev=1):
    return hub.open(slug, lambda: {"ledger": ledger(rev), "swarm": None, "workspaces": {}}, cursor)


def test_a_first_connection_gets_one_snapshot_at_the_current_cursor():
    hub = Hub()
    seq, first = opened(hub)
    channel = hub.channels[SLUG]
    assert seq == 0
    assert first == [
        (0, f"{channel.epoch}.0", "snapshot", {"ledger": ledger(1), "swarm": None, "workspaces": {}}),
    ]
    assert hub.watched() == [SLUG]


def test_every_channel_life_has_its_own_epoch():
    first, second = Hub(), Hub()
    opened(first)
    opened(second)
    assert first.channels[SLUG].epoch != second.channels[SLUG].epoch


def test_a_second_connection_reuses_the_loaded_resources():
    hub = Hub()
    opened(hub)
    hub.publish(SLUG, "ledger", ledger(2))
    calls = []
    seq, first = hub.open(SLUG, lambda: calls.append(1) or {"ledger": ledger(1)})
    assert calls == [] and seq == 1
    assert first[0][3]["ledger"] == ledger(2)


def test_a_publish_appends_a_patch_that_rebuilds_the_new_value():
    hub = Hub()
    seq, _ = opened(hub)
    assert hub.publish(SLUG, "ledger", ledger(2, tasks=[{"id": "t1"}]))
    events = hub.wait(SLUG, seq, 0.01)
    channel = hub.channels[SLUG]
    assert [(at, cursor, name, data["rev"]) for at, cursor, name, data in events] == [
        (1, f"{channel.epoch}.1", "ledger", 2)
    ]
    assert apply(ledger(1), events[0][3]["patch"]) == ledger(2, tasks=[{"id": "t1"}])
    assert hub.resource(SLUG, "ledger") == ledger(2, tasks=[{"id": "t1"}])


def test_publishing_an_unchanged_value_or_an_unwatched_ledger_adds_nothing():
    hub = Hub()
    assert not hub.publish(SLUG, "ledger", ledger(1))
    assert not hub.has(SLUG)
    opened(hub)
    assert not hub.publish(SLUG, "ledger", ledger(1))
    assert hub.channels[SLUG].seq == 0
    assert list(hub.channels[SLUG].log) == []


def test_a_first_value_for_a_resource_is_a_baseline_without_an_event():
    hub = Hub()
    hub.open(SLUG, lambda: {"ledger": ledger(1)})
    assert not hub.publish(SLUG, "swarm", {"config": {}})
    assert hub.resource(SLUG, "swarm") == {"config": {}}
    assert hub.channels[SLUG].seq == 0
    assert hub.publish(SLUG, "swarm", {"config": {"state": "running"}})
    assert hub.channels[SLUG].log[-1][2:] == ("swarm", {"patch": {"o": {"config": {"o": {"state": {"v": "running"}}}}}})


def test_a_resource_loaded_as_none_still_streams_its_first_value():
    hub = Hub()
    opened(hub)
    assert hub.publish(SLUG, "swarm", {"config": {"state": "paused"}})
    assert apply(None, hub.channels[SLUG].log[-1][3]["patch"]) == {"config": {"state": "paused"}}


def test_an_older_ledger_revision_never_overwrites_a_newer_one():
    hub = Hub()
    opened(hub)
    assert hub.publish(SLUG, "ledger", ledger(3))
    assert not hub.publish(SLUG, "ledger", ledger(2))
    assert hub.resource(SLUG, "ledger") == ledger(3)
    assert hub.channels[SLUG].seq == 1
    assert hub.publish(SLUG, "ledger", ledger(3, title="same rev, new meta"))
    assert hub.channels[SLUG].log[-1][3]["rev"] == 3


def test_other_resources_carry_no_revision():
    hub = Hub()
    opened(hub, rev=5)
    assert hub.publish(SLUG, "workspaces", {"t1": ["line"]})
    assert hub.channels[SLUG].log[-1][3] == {"patch": {"o": {"t1": {"v": ["line"]}}}}


def test_concurrent_publishes_leave_an_ordered_log_that_replays_to_the_newest():
    hub = Hub()
    opened(hub)
    threads = [threading.Thread(target=hub.publish, args=(SLUG, "ledger", ledger(rev))) for rev in range(2, 40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    revs = [data["rev"] for _, _, _, data in hub.channels[SLUG].log]
    assert revs == sorted(revs) and revs[-1] == 39
    value = ledger(1)
    for _, _, _, data in hub.channels[SLUG].log:
        value = apply(value, data["patch"])
    assert value == ledger(39)


def test_a_publish_builds_its_patch_while_holding_the_channel_lock(monkeypatch):
    hub = Hub()
    opened(hub)
    real_diff = events_hub.patch.diff
    held = []

    def try_lock():
        got = hub.changed.acquire(blocking=False)
        held.append(not got)
        if got:
            hub.changed.release()

    def diff(old, new):
        other = threading.Thread(target=try_lock)
        other.start()
        other.join()
        return real_diff(old, new)

    monkeypatch.setattr(events_hub.patch, "diff", diff)
    hub.publish(SLUG, "ledger", ledger(2))
    assert held and all(held)


def test_a_reconnect_replays_exactly_the_events_after_its_cursor():
    hub = Hub()
    opened(hub)
    for rev in (2, 3, 4):
        hub.publish(SLUG, "ledger", ledger(rev))
    cursor = hub.channels[SLUG].log[0][1]
    seq, replay = hub.open(SLUG, None, cursor)
    assert seq == 1
    assert [data["rev"] for _, _, _, data in replay] == [3, 4]
    latest = hub.channels[SLUG].log[-1][1]
    assert hub.open(SLUG, None, latest) == (3, [])


def test_a_cursor_from_the_snapshot_replays_everything_since():
    hub = Hub()
    _, first = opened(hub)
    assert hub.open(SLUG, None, first[0][1]) == (0, [])
    hub.publish(SLUG, "ledger", ledger(2))
    _, replay = hub.open(SLUG, None, first[0][1])
    assert [data["rev"] for _, _, _, data in replay] == [2]


@pytest.mark.parametrize("seq", ["", "x", "1x", "-1", "1.0", "²"])
def test_an_unreadable_cursor_is_expired(seq):
    hub = Hub()
    opened(hub)
    hub.publish(SLUG, "ledger", ledger(2))
    with pytest.raises(Expired):
        hub.open(SLUG, None, f"{hub.channels[SLUG].epoch}.{seq}")
    with pytest.raises(Expired):
        hub.open(SLUG, None, f"{hub.channels[SLUG].epoch}{seq}")


def test_a_cursor_from_another_server_ledger_or_channel_life_is_expired():
    hub = Hub()
    opened(hub)
    opened(hub, "other-2026-01-01")
    other = Hub()
    opened(other)
    with pytest.raises(Expired):
        hub.open(SLUG, None, Hub.cursor(other.channels[SLUG], 0))
    with pytest.raises(Expired):
        hub.open(SLUG, None, Hub.cursor(hub.channels["other-2026-01-01"], 0))
    with pytest.raises(Expired):
        hub.open("never-2026-01-01", None, Hub.cursor(hub.channels[SLUG], 0))
    old = Hub.cursor(hub.channels[SLUG], 0)
    hub.close(SLUG)
    hub.evict(now=time.monotonic() + events_hub.IDLE_EVICT_S)
    with pytest.raises(Expired):
        hub.open(SLUG, None, old)
    opened(hub)
    with pytest.raises(Expired):
        hub.open(SLUG, None, old)


def test_a_cursor_ahead_of_the_channel_is_expired():
    hub = Hub()
    opened(hub)
    with pytest.raises(Expired):
        hub.open(SLUG, None, Hub.cursor(hub.channels[SLUG], 1))


def test_a_cursor_older_than_retention_is_expired_and_the_oldest_kept_one_replays():
    hub = Hub(retained=3)
    opened(hub)
    channel = hub.channels[SLUG]
    for rev in range(2, 14):
        hub.publish(SLUG, "ledger", ledger(rev))
    assert [at for at, *_ in channel.log] == [10, 11, 12]
    for old in (8, 1):
        with pytest.raises(Expired):
            hub.open(SLUG, None, Hub.cursor(channel, old))
    seq, replay = hub.open(SLUG, None, Hub.cursor(channel, 9))
    assert seq == 9 and [at for at, *_ in replay] == [10, 11, 12]
    assert hub.open(SLUG, None, Hub.cursor(channel, 11)) == (11, [channel.log[-1]])


def test_wait_returns_nothing_after_its_timeout():
    hub = Hub()
    opened(hub)
    events, took = bounded(hub.wait, SLUG, 0, 0.05)
    assert events == [] and took >= 0.05


def woken_by_a_publish():
    hub = Hub()
    opened(hub)
    timer = threading.Timer(0.05, hub.publish, args=(SLUG, "ledger", ledger(2)))
    timer.start()
    events, took = bounded(hub.wait, SLUG, 0, 1.5)
    timer.join()
    return [at for at, *_ in events], took


def answered_from_kept_events():
    hub = Hub()
    opened(hub)
    for rev in (2, 3, 4):
        hub.publish(SLUG, "ledger", ledger(rev))
    events, took = bounded(hub.wait, SLUG, 2, 1.5)
    return [at for at, *_ in events], took


def test_wait_wakes_on_a_publish():
    assert woken_by_a_publish()[0] == [1]


@pytest.mark.wall_clock
def test_wait_wakes_at_once_on_a_publish():
    assert woken_by_a_publish()[1] < 1


def test_wait_answers_when_events_are_already_kept():
    assert answered_from_kept_events()[0] == [3]


@pytest.mark.wall_clock
def test_wait_answers_at_once_when_events_are_already_kept():
    assert answered_from_kept_events()[1] < 1


def test_wait_raises_expired_when_a_slow_reader_fell_out_of_retention():
    hub = Hub(retained=2)
    opened(hub)
    for rev in range(2, 6):
        hub.publish(SLUG, "ledger", ledger(rev))
    with pytest.raises(Expired):
        hub.wait(SLUG, 1, 0.01)
    assert [at for at, *_ in hub.wait(SLUG, 2, 0.01)] == [3, 4]
    assert [at for at, *_ in hub.wait(SLUG, 3, 0.01)] == [4]


@pytest.mark.parametrize("closed_at", [250.3 + offset for offset in range(20)])
def test_closing_the_last_stream_stops_sampling_and_eviction_waits_for_the_grace_period(closed_at):
    now = [closed_at - 10]
    hub = Hub(clock=lambda: now[0])
    opened(hub)
    opened(hub)
    hub.close(SLUG)
    assert hub.watched() == [SLUG]
    now[0] = closed_at
    hub.close(SLUG)
    assert hub.watched() == []
    deadline = closed_at + events_hub.IDLE_EVICT_S
    now[0] = deadline - 1
    hub.evict()
    assert hub.has(SLUG)
    now[0] = deadline
    hub.evict()
    assert not hub.has(SLUG)
    assert hub.resource(SLUG, "ledger") is None


def test_eviction_defaults_to_now_and_the_grace_period(monkeypatch):
    now = [250.3]
    monkeypatch.setattr(events_hub.time, "monotonic", lambda: now[0])
    hub = Hub()
    opened(hub)
    hub.close(SLUG)
    hub.evict()
    assert hub.has(SLUG)
    now[0] += events_hub.IDLE_EVICT_S
    hub.evict()
    assert not hub.has(SLUG)


@pytest.mark.parametrize("reconnect", [False, True])
def test_a_watched_ledger_is_never_evicted(reconnect):
    now = [250.3]
    hub = Hub(clock=lambda: now[0])
    opened(hub)
    if reconnect:
        hub.close(SLUG)
        now[0] += events_hub.IDLE_EVICT_S - 1
        opened(hub)
    now[0] += 10 * events_hub.IDLE_EVICT_S
    hub.evict()
    assert hub.has(SLUG)
    hub.close(SLUG)
    hub.evict()
    assert hub.has(SLUG)
    now[0] += events_hub.IDLE_EVICT_S
    hub.evict()
    assert not hub.has(SLUG)


def test_a_failed_load_unsubscribes_and_raises():
    hub = Hub()

    def broken():
        raise OSError("unreadable")

    with pytest.raises(OSError):
        hub.open(SLUG, broken)
    assert hub.watched() == []


def test_a_publish_during_the_first_load_wins_over_the_loaded_copy():
    hub = Hub()

    def load():
        hub.publish(SLUG, "ledger", ledger(5))
        return {"ledger": ledger(4), "swarm": None}

    _, first = hub.open(SLUG, load)
    assert first[0][3] == {"ledger": ledger(5), "swarm": None}


def test_the_default_retention_is_bounded():
    hub = Hub()
    opened(hub)
    assert hub.channels[SLUG].log.maxlen == events_hub.RETAINED
