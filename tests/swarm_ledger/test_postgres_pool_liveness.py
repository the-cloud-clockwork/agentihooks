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
        self.raw.statements.append(sql)

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
        self.statements: list[str] = []

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
