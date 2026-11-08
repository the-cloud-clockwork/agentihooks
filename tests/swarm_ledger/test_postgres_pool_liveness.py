from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import psycopg2
import pytest

from scripts.swarm import ledger_probe
from scripts.swarm_ledger.repository import postgres


class Cursor:
    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        if self.raw.dead:
            raise psycopg2.OperationalError("server closed connection")
        self.raw.statements.append((sql, parameters))

    def fetchone(self) -> tuple[int, int]:
        return (100, 3)

    def close(self) -> None:
        pass

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


class Raw:
    def __init__(self) -> None:
        self.closed = False
        self.dead = False
        self.statements: list[tuple[str, Any]] = []

    def cursor(self) -> Cursor:
        return Cursor(self)

    def rollback(self) -> None:
        if self.dead:
            raise psycopg2.InterfaceError("connection already closed")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def raws(monkeypatch: pytest.MonkeyPatch) -> list[Raw]:
    made: list[Raw] = []

    def connect(*args: Any, **kwargs: Any) -> Raw:
        raw = Raw()
        made.append(raw)
        return raw

    monkeypatch.setattr(psycopg2, "connect", connect)
    monkeypatch.setattr(postgres, "POOLS", {})
    return made


@pytest.mark.feature("ledger-pool-liveness")
def test_checkout_replaces_a_connection_killed_by_database_restart(raws: list[Raw]) -> None:
    pool = postgres.Pool("database")
    first = pool.take()
    pool.give(first)
    first.dead = True
    replacement = pool.take()
    replacement.cursor().execute("INSERT INTO ledger VALUES (1)")
    assert replacement is not first
    assert first.closed
    assert len(raws) == 2
    pool.give(replacement)


@pytest.mark.feature("ledger-pool-liveness")
def test_pool_bounds_checked_out_connections_by_server_share(raws: list[Raw], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_POOL_SHARE", "0.02")
    pool = postgres.Pool("database")
    first, second = pool.take(), pool.take()
    started, acquired = Event(), Event()

    def take() -> Any:
        started.set()
        raw = pool.take()
        acquired.set()
        return raw

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(take)
        assert started.wait(1)
        try:
            assert not acquired.wait(0.1)
            assert len(raws) == 2
        finally:
            pool.give(first)
        assert pending.result(timeout=2) is first
    pool.give(first)
    pool.give(second)


@pytest.mark.feature("ledger-pool-liveness")
def test_failed_rollback_is_recorded_as_a_probe_failure(raws: list[Raw]) -> None:
    def write() -> None:
        connection = postgres.Connection("database")
        try:
            with connection:
                connection.raw.dead = True
                connection.execute("INSERT INTO ledger VALUES (1)")
        finally:
            connection.close()

    elapsed, failure, refusal = ledger_probe._timed(write, lambda: 0.0)
    assert (elapsed, failure, refusal) == (0.0, ledger_probe.NO_ANSWER, "")


@pytest.mark.feature("ledger-pool-liveness")
def test_pool_notifications_do_not_extend_the_checkout_deadline(
    raws: list[Raw], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_POOL_SHARE", "0.01")
    pool = postgres.Pool("database")
    raw = pool.take()
    clock = iter((0.0, 1.0, 4.5, 5.0))
    waits: list[float] = []

    def wait(timeout: float) -> bool:
        waits.append(timeout)
        return True

    monkeypatch.setattr(postgres.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(pool.lock, "wait", wait)
    with pytest.raises(postgres.sqlite3.OperationalError, match="^ledger database pool capacity exhausted$"):
        pool.take()
    assert waits == [4.0, 0.5]
    assert len(raws) == 1
    pool.give(raw)


@pytest.mark.feature("ledger-pool-liveness")
def test_a_slow_connect_does_not_hold_the_pool_lock(raws: list[Raw], monkeypatch: pytest.MonkeyPatch) -> None:
    pool = postgres.Pool("database")
    first = pool.take()
    connecting, release = Event(), Event()

    def slow(*args: Any, **kwargs: Any) -> Raw:
        connecting.set()
        assert release.wait(2)
        raw = Raw()
        raws.append(raw)
        return raw

    monkeypatch.setattr(psycopg2, "connect", slow)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(pool.take)
        assert connecting.wait(1)
        assert pool.opened == 2
        pool.give(first)
        assert pool.take() is first
        release.set()
        opened = pending.result(timeout=2)
    assert opened is raws[-1] and pool.opened == 2 and opened.statements == []
    pool.give(first)
    pool.give(opened)


CAPACITY = (
    "SELECT current_setting('max_connections')::int, current_setting('superuser_reserved_connections')::int",
    (),
)


@pytest.mark.feature("ledger-pool-liveness")
def test_only_the_first_connection_measures_the_server_and_reuse_checks_liveness(raws: list[Raw]) -> None:
    pool = postgres.Pool("database")
    first = pool.take()
    second = pool.take()
    assert first.statements == [CAPACITY] and second.statements == []
    pool.give(first)
    assert pool.take() is first
    assert first.statements == [CAPACITY, (postgres.LIVENESS, ())]
    pool.give(first)
    pool.give(second)


@pytest.mark.feature("ledger-pool-liveness")
def test_a_closed_idle_connection_is_replaced_and_frees_its_slot(raws: list[Raw]) -> None:
    pool = postgres.Pool("database")
    first = pool.take()
    pool.give(first)
    first.close()
    replacement = pool.take()
    assert replacement is not first and pool.opened == 1
    assert first.statements == [CAPACITY]
    pool.give(replacement)


@pytest.mark.feature("ledger-pool-liveness")
def test_giving_back_a_closed_connection_frees_exactly_its_slot(raws: list[Raw]) -> None:
    pool = postgres.Pool("database")
    first, second, third = pool.take(), pool.take(), pool.take()
    second.close()
    pool.give(second)
    assert pool.opened == 2 and pool.idle == []
    pool.give(first)
    assert pool.opened == 2 and pool.idle == [first]
    pool.give(third)


@pytest.mark.feature("ledger-pool-liveness")
def test_takers_wait_for_the_first_connection_to_measure_the_server(
    raws: list[Raw], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = postgres.Pool("database")
    connecting, release, early = Event(), Event(), Event()
    made: list[Raw] = []

    def slow(*args: Any, **kwargs: Any) -> Raw:
        raw = Raw()
        made.append(raw)
        if len(made) == 1:
            connecting.set()
            assert release.wait(2)
        elif not release.is_set():
            early.set()
        return raw

    monkeypatch.setattr(psycopg2, "connect", slow)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(pool.take)
        assert connecting.wait(1)
        second = executor.submit(pool.take)
        assert not early.wait(0.2) and not second.done()
        release.set()
        taken = first.result(timeout=2), second.result(timeout=2)
    assert taken == (made[0], made[1]) and pool.opened == 2
    for raw in taken:
        pool.give(raw)


@pytest.mark.feature("ledger-pool-liveness")
def test_a_failed_connect_returns_its_reserved_slot(raws: list[Raw], monkeypatch: pytest.MonkeyPatch) -> None:
    pool = postgres.Pool("database")
    first = pool.take()

    def refused(*args: Any, **kwargs: Any) -> Raw:
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(psycopg2, "connect", refused)
    with pytest.raises(postgres.sqlite3.DatabaseError):
        pool.take()
    assert pool.opened == 1
    pool.give(first)


@pytest.mark.parametrize("share, cap", [("0.1", 10), ("0.5", 50), ("0.99", 96)])
def test_server_reservations_and_configured_share_bound_the_pool(
    raws: list[Raw], monkeypatch: pytest.MonkeyPatch, share: str, cap: int
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_POOL_SHARE", share)
    pool = postgres.Pool("database")
    raw = pool.take()
    assert pool.cap == cap
    pool.give(raw)


@pytest.mark.parametrize("share", ["0", "1", "-0.1", "nan"])
def test_an_invalid_share_closes_the_bootstrap_connection(
    raws: list[Raw], monkeypatch: pytest.MonkeyPatch, share: str
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_POOL_SHARE", share)
    pool = postgres.Pool("database")
    with pytest.raises(postgres.sqlite3.OperationalError, match="^ledger pool share must be between zero and one$"):
        pool.take()
    assert raws[0].closed
    assert pool.opened == 0
