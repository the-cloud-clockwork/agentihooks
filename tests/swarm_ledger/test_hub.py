import threading
import time

import pytest

from scripts.swarm_ledger.events import hub as events_hub
from scripts.swarm_ledger.events.hub import Expired, Hub
from scripts.swarm_ledger.events.patch import apply

SLUG = "hub-2026-01-01"


def ledger(rev, **fields):
    return {"tasks": [], **fields, "_meta": {"rev": rev}}


def opened(hub, slug=SLUG, cursor=None, rev=1):
    return hub.open(slug, lambda: {"ledger": ledger(rev), "swarm": None, "workspaces": {}}, cursor)


def test_a_first_connection_gets_one_snapshot_at_the_current_cursor():
    hub = Hub(boot="b")
    seq, first = opened(hub)
    assert seq == 0
    assert first == [
        (0, hub.cursor(SLUG, hub.channels[SLUG], 0), "snapshot", {"ledger": ledger(1), "swarm": None, "workspaces": {}})
    ]
    assert hub.watched() == [SLUG]


def test_a_second_connection_reuses_the_loaded_resources():
    hub = Hub(boot="b")
    opened(hub)
    hub.publish(SLUG, "ledger", ledger(2))
    calls = []
    seq, first = hub.open(SLUG, lambda: calls.append(1) or {"ledger": ledger(1)})
    assert calls == [] and seq == 1
    assert first[0][3]["ledger"] == ledger(2)


def test_a_publish_appends_a_patch_that_rebuilds_the_new_value():
    hub = Hub(boot="b")
    seq, _ = opened(hub)
    assert hub.publish(SLUG, "ledger", ledger(2, tasks=[{"id": "t1"}]))
    events = hub.wait(SLUG, seq, 0.01)
    assert [(at, name, data["rev"]) for at, _, name, data in events] == [(1, "ledger", 2)]
    assert apply(ledger(1), events[0][3]["patch"]) == ledger(2, tasks=[{"id": "t1"}])


def test_publishing_an_unchanged_value_or_an_unwatched_ledger_adds_nothing():
    hub = Hub(boot="b")
    assert not hub.publish(SLUG, "ledger", ledger(1))
    assert not hub.has(SLUG)
    opened(hub)
    assert not hub.publish(SLUG, "ledger", ledger(1))
    assert hub.channels[SLUG].seq == 0


def test_a_first_value_for_a_resource_is_a_baseline_without_an_event():
    hub = Hub(boot="b")
    hub.open(SLUG, lambda: {"ledger": ledger(1)})
    assert not hub.publish(SLUG, "swarm", {"config": {}})
    assert hub.resource(SLUG, "swarm") == {"config": {}}
    assert hub.publish(SLUG, "swarm", {"config": {"state": "running"}})
    assert hub.channels[SLUG].log[-1][3] == {"patch": {"o": {"config": {"o": {"state": {"v": "running"}}}}}}


def test_an_older_ledger_revision_never_overwrites_a_newer_one():
    hub = Hub(boot="b")
    opened(hub)
    assert hub.publish(SLUG, "ledger", ledger(3))
    assert not hub.publish(SLUG, "ledger", ledger(2))
    assert hub.resource(SLUG, "ledger") == ledger(3)
    assert hub.publish(SLUG, "ledger", ledger(3, title="same rev, new meta"))
    assert hub.channels[SLUG].log[-1][3]["rev"] == 3


def test_concurrent_publishes_leave_an_ordered_log_that_replays_to_the_newest():
    hub = Hub(boot="b")
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


def test_a_publish_racing_another_rebuilds_its_patch_against_the_newer_copy(monkeypatch):
    hub = Hub(boot="b")
    opened(hub)
    real_diff = events_hub.patch.diff
    raced = []

    def diff(old, new):
        if not raced:
            raced.append(1)
            hub.publish(SLUG, "ledger", ledger(2, title="first"))
        return real_diff(old, new)

    monkeypatch.setattr(events_hub.patch, "diff", diff)
    assert hub.publish(SLUG, "ledger", ledger(3, title="second"))
    log = hub.channels[SLUG].log
    assert [data["rev"] for _, _, _, data in log] == [2, 3]
    value = ledger(1)
    for _, _, _, data in log:
        value = apply(value, data["patch"])
    assert value == ledger(3, title="second")


def test_a_publish_racing_a_newer_revision_drops_the_older_one(monkeypatch):
    hub = Hub(boot="b")
    opened(hub)
    real_diff = events_hub.patch.diff
    raced = []

    def diff(old, new):
        if not raced:
            raced.append(1)
            hub.publish(SLUG, "ledger", ledger(5))
        return real_diff(old, new)

    monkeypatch.setattr(events_hub.patch, "diff", diff)
    assert not hub.publish(SLUG, "ledger", ledger(4))
    assert hub.resource(SLUG, "ledger") == ledger(5)
    assert [data["rev"] for _, _, _, data in hub.channels[SLUG].log] == [5]


def test_a_publish_whose_channel_was_evicted_mid_diff_stops(monkeypatch):
    hub = Hub(boot="b")
    opened(hub)
    hub.close(SLUG)
    real_diff = events_hub.patch.diff

    def diff(old, new):
        hub.evict(now=time.monotonic() + events_hub.IDLE_EVICT_S)
        return real_diff(old, new)

    monkeypatch.setattr(events_hub.patch, "diff", diff)
    assert not hub.publish(SLUG, "ledger", ledger(2))
    assert not hub.has(SLUG)


def test_a_reconnect_replays_exactly_the_events_after_its_cursor():
    hub = Hub(boot="b")
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
    hub = Hub(boot="b")
    _, first = opened(hub)
    hub.publish(SLUG, "ledger", ledger(2))
    _, replay = hub.open(SLUG, None, first[0][1])
    assert [data["rev"] for _, _, _, data in replay] == [2]


@pytest.mark.parametrize("cursor", ["", "not base64 !", "Zm9v", "YjplOnM6eA"])
def test_an_unreadable_cursor_is_expired(cursor):
    hub = Hub(boot="b")
    opened(hub)
    with pytest.raises(Expired):
        hub.open(SLUG, None, cursor)


def test_a_cursor_from_another_server_ledger_or_channel_is_expired():
    hub = Hub(boot="b")
    opened(hub)
    opened(hub, "other-2026-01-01")
    channel = hub.channels[SLUG]
    with pytest.raises(Expired):
        hub.open(SLUG, None, Hub(boot="c").cursor(SLUG, channel, 0))
    with pytest.raises(Expired):
        hub.open(SLUG, None, hub.cursor("other-2026-01-01", channel, 0))
    old = hub.cursor(SLUG, channel, 0)
    hub.close(SLUG)
    hub.evict(now=time.monotonic() + events_hub.IDLE_EVICT_S)
    with pytest.raises(Expired):
        hub.open(SLUG, None, old)
    opened(hub)
    with pytest.raises(Expired):
        hub.open(SLUG, None, old)


def test_a_cursor_ahead_of_the_channel_is_expired():
    hub = Hub(boot="b")
    opened(hub)
    with pytest.raises(Expired):
        hub.open(SLUG, None, hub.cursor(SLUG, hub.channels[SLUG], 1))


def test_a_cursor_older_than_retention_is_expired_and_the_oldest_kept_one_replays():
    hub = Hub(retained=3, boot="b")
    opened(hub)
    channel = hub.channels[SLUG]
    for rev in range(2, 8):
        hub.publish(SLUG, "ledger", ledger(rev))
    assert [at for at, *_ in channel.log] == [4, 5, 6]
    with pytest.raises(Expired):
        hub.open(SLUG, None, hub.cursor(SLUG, channel, 2))
    seq, replay = hub.open(SLUG, None, hub.cursor(SLUG, channel, 3))
    assert seq == 3 and [at for at, *_ in replay] == [4, 5, 6]


def test_wait_returns_nothing_after_its_timeout_and_wakes_on_a_publish():
    hub = Hub(boot="b")
    opened(hub)
    started = time.monotonic()
    assert hub.wait(SLUG, 0, 0.05) == []
    assert time.monotonic() - started >= 0.05
    timer = threading.Timer(0.05, hub.publish, args=(SLUG, "ledger", ledger(2)))
    timer.start()
    events = hub.wait(SLUG, 0, 5)
    timer.join()
    assert [at for at, *_ in events] == [1]


def test_wait_raises_expired_when_a_slow_reader_fell_out_of_retention():
    hub = Hub(retained=2, boot="b")
    opened(hub)
    for rev in range(2, 6):
        hub.publish(SLUG, "ledger", ledger(rev))
    with pytest.raises(Expired):
        hub.wait(SLUG, 1, 0.01)
    assert [at for at, *_ in hub.wait(SLUG, 2, 0.01)] == [3, 4]


def test_closing_the_last_stream_stops_sampling_and_eviction_waits_for_the_grace_period():
    hub = Hub(boot="b")
    opened(hub)
    opened(hub)
    hub.close(SLUG)
    assert hub.watched() == [SLUG]
    hub.close(SLUG)
    assert hub.watched() == []
    closed = hub.channels[SLUG].idle_since
    hub.evict(now=closed + events_hub.IDLE_EVICT_S - 1)
    assert hub.has(SLUG)
    hub.evict(now=closed + events_hub.IDLE_EVICT_S)
    assert not hub.has(SLUG)
    assert hub.resource(SLUG, "ledger") is None


def test_a_watched_ledger_is_never_evicted():
    hub = Hub(boot="b")
    opened(hub)
    hub.evict(now=time.monotonic() + 10 * events_hub.IDLE_EVICT_S)
    assert hub.has(SLUG)


def test_a_failed_load_unsubscribes_and_raises():
    hub = Hub(boot="b")

    def broken():
        raise OSError("unreadable")

    with pytest.raises(OSError):
        hub.open(SLUG, broken)
    assert hub.watched() == []


def test_a_publish_during_the_first_load_wins_over_the_loaded_copy():
    hub = Hub(boot="b")

    def load():
        hub.publish(SLUG, "ledger", ledger(5))
        return {"ledger": ledger(4), "swarm": None}

    _, first = hub.open(SLUG, load)
    assert first[0][3] == {"ledger": ledger(5), "swarm": None}


def test_the_default_hub_has_its_own_boot_id():
    assert Hub().boot != Hub().boot
    assert len(Hub().boot) == 16
