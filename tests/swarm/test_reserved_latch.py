import time

import pytest

from scripts.routing import aggregate, reservation
from scripts.swarm import capacity
from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import RedisStore

NOW = 1_800_000_000
RESET = NOW + 86_400


@pytest.fixture
def world(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    rows, masters = [], {"claude": ("main", 2)}
    monkeypatch.setattr(capacity, "_claude", lambda environ, now: [row for row in rows if row.harness == "claude"])
    monkeypatch.setattr(capacity, "_codex", lambda environ, now, refresh: [r for r in rows if r.harness == "codex"])
    monkeypatch.setattr(
        reservation,
        "current",
        lambda harness, environ: reservation.Reservation(harness, *masters[harness]) if harness in masters else None,
    )
    monkeypatch.setattr(reservation, "_store", lambda environ: store if environ is not None else None)
    monkeypatch.setattr(aggregate, "enabled", lambda environ=None: False)
    return store, rows, masters


def _row(name="main", week_left=39.0, harness="claude", kind=capacity.SUBSCRIPTION, reset=RESET):
    return capacity.Account(harness, name, "OPEN", 1, 90.0, week_left, 6, reset, kind=kind)


def test_a_reserved_account_stays_closed_for_workers_when_the_live_master_count_dips(world):
    _, rows, masters = world
    rows += [_row(), _row("other")]

    first = capacity.accounts({}, NOW, refresh=False)
    masters["claude"] = ("main", 1)
    second = capacity.accounts({}, NOW + 60, refresh=False)

    assert [(row.name, row.state, row.cap) for row in first] == [("main", "CLOSED", 0), ("other", "OPEN", 6)]
    assert [(row.name, row.state, row.cap) for row in second] == [("main", "CLOSED", 0), ("other", "OPEN", 6)]
    assert second[0].reserved == reservation.reason(2)
    assert second[0].unreserved_cap == 6
    assert [(row.name, row.state, row.cap) for row in capacity.unreserved(second)][0] == ("main", "OPEN", 6)


def test_a_remembered_closure_keeps_every_other_row_in_place(world):
    _, rows, masters = world
    rows += [_row("before"), _row(), _row("after")]
    capacity.accounts({}, NOW, refresh=False)
    masters["claude"] = ("main", 1)

    assert [(row.name, row.state) for row in capacity.accounts({}, NOW + 60, refresh=False)] == [
        ("before", "OPEN"),
        ("main", "CLOSED"),
        ("after", "OPEN"),
    ]


def test_the_closure_lasts_until_the_account_week_resets(world):
    store, rows, _ = world
    reset = int(time.time()) + 3600
    rows.append(_row(reset=reset))

    capacity.accounts({}, NOW, refresh=False)

    assert 3500 < store.redis.ttl(f"{ROOT}:reserved-closed:claude:main") <= 3600


def test_an_account_below_its_worker_stop_is_never_closed(world):
    _, rows, masters = world
    rows.append(_row(week_left=45.0))
    masters["claude"] = ("main", 1)

    assert [(row.state, row.cap, row.reserved) for row in capacity.accounts({}, NOW, refresh=False)] == [
        ("OPEN", 6, "")
    ]


def test_a_redeclared_master_account_releases_the_old_closure(world):
    _, rows, masters = world
    rows += [_row(), _row("next", week_left=90.0)]
    capacity.accounts({}, NOW, refresh=False)
    masters["claude"] = ("next", 1)

    assert [(row.name, row.state) for row in capacity.accounts({}, NOW + 60, refresh=False)] == [
        ("main", "OPEN"),
        ("next", "OPEN"),
    ]


def test_clearing_the_master_declaration_releases_the_closure(world):
    _, rows, masters = world
    rows.append(_row())
    capacity.accounts({}, NOW, refresh=False)
    masters.clear()

    assert [(row.state, row.cap, row.reserved) for row in capacity.accounts({}, NOW + 60, refresh=False)] == [
        ("OPEN", 6, "")
    ]


def test_the_closure_never_reaches_another_harness_or_an_api_row(world):
    _, rows, masters = world
    rows.append(_row())
    capacity.accounts({}, NOW, refresh=False)
    rows[:] = [_row(harness="codex", week_left=90.0), _row(kind=capacity.API, week_left=90.0)]
    masters["claude"] = ("main", 1)

    assert [(row.harness, row.kind, row.state) for row in capacity.accounts({}, NOW + 60, refresh=False)] == [
        ("claude", capacity.API, "OPEN"),
        ("codex", capacity.SUBSCRIPTION, "OPEN"),
    ]


def test_without_a_reachable_store_the_live_reservation_still_closes_the_account(world, monkeypatch):
    _, rows, _ = world
    rows.append(_row())
    monkeypatch.setattr(reservation, "_store", lambda environ: None)

    assert [(row.state, row.cap) for row in capacity.accounts({}, NOW, refresh=False)] == [("CLOSED", 0)]


def test_a_closure_without_a_known_week_reset_is_not_remembered(world):
    store, rows, masters = world
    rows.append(_row(reset=None))
    capacity.accounts({}, NOW, refresh=False)
    masters["claude"] = ("main", 1)

    assert store.redis.keys(f"{ROOT}:reserved-closed:*") == []
    assert [row.state for row in capacity.accounts({}, NOW + 60, refresh=False)] == ["OPEN"]


def test_a_redis_failure_leaves_the_live_reservation_in_charge(world, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    store, rows, masters = world
    rows.append(_row())

    def down(*args, **kwargs):
        raise RedisConnectionError("down")

    monkeypatch.setattr(store.redis, "set", down)
    monkeypatch.setattr(store.redis, "get", down)

    assert [row.state for row in capacity.accounts({}, NOW, refresh=False)] == ["CLOSED"]
    masters["claude"] = ("main", 1)
    assert [row.state for row in capacity.accounts({}, NOW + 60, refresh=False)] == ["OPEN"]
