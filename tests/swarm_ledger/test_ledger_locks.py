import threading
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger import new_ledger, request_metrics
from scripts.swarm_ledger.ledger_locks import LedgerLock
from scripts.swarm_ledger.repository import postgres
from scripts.swarm_ledger.repository.sqlite import BEGIN_IMMEDIATE, SQLiteLedgerRepository

PATIENCE = 10


def runs(target, patience=0.3):
    done = threading.Event()

    def run():
        target()
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return done.wait(patience), thread


def test_two_ledgers_hold_at_once_and_one_ledger_holds_alone():
    lock = LedgerLock()
    held = lock.ledger("one")
    held.acquire()
    assert lock.locked()
    assert runs(lock.ledger("two").acquire)[0]
    lock.ledger("two").release()
    same, thread = runs(lock.ledger("one").acquire)
    assert not same
    held.release()
    thread.join(PATIENCE)
    assert lock.held["one"] == 1
    lock.ledger("one").release()
    assert not lock.locked()


def test_the_whole_lock_waits_for_ledgers_and_blocks_them():
    lock = LedgerLock()
    held = lock.ledger("one")
    held.acquire()
    whole, thread = runs(lock.acquire)
    assert not whole
    assert not lock.acquire(blocking=False)
    queued, behind = runs(lock.ledger("two").acquire)
    assert not queued
    held.release()
    thread.join(PATIENCE)
    assert lock.whole
    assert not runs(lock.ledger("three").acquire)[0]
    lock.release()
    behind.join(PATIENCE)
    assert lock.held["two"] == 1


def test_the_whole_lock_refuses_a_release_it_does_not_hold():
    with pytest.raises(RuntimeError):
        LedgerLock().release()


def test_begin_ledger_takes_a_ledger_lock_where_the_store_has_one():
    taken = []
    timing = request_metrics.Timing()
    token = request_metrics.CURRENT.set(timing)
    try:
        request_metrics.begin_ledger(SimpleNamespace(lock_ledger=taken.append), "one", BEGIN_IMMEDIATE)
        request_metrics.begin_ledger(SimpleNamespace(execute=taken.append), "one", BEGIN_IMMEDIATE)
    finally:
        request_metrics.CURRENT.reset(token)
    assert taken == ["one", BEGIN_IMMEDIATE]
    assert timing.locking == 0


def test_a_postgres_ledger_lock_shares_the_store_lock_and_holds_its_ledger(monkeypatch):
    executed = []

    class Cursor:
        def execute(self, sql, parameters):
            executed.append((sql, parameters))

    failure = type("Failure", (Exception,), {})
    monkeypatch.setattr(
        postgres,
        "errors",
        lambda: SimpleNamespace(
            errors=SimpleNamespace(LockNotAvailable=failure, UndefinedTable=failure),
            extensions=SimpleNamespace(TRANSACTION_STATUS_IDLE=0),
            Error=failure,
            IntegrityError=failure,
        ),
    )
    connection = postgres.Connection.__new__(postgres.Connection)
    connection.raw = SimpleNamespace(get_transaction_status=lambda: 0, cursor=Cursor)
    connection.lock_ledger("one")
    assert executed == [
        (f"SELECT pg_advisory_xact_lock_shared({postgres.WRITER_LOCK})", ()),
        (f"SELECT pg_advisory_xact_lock({postgres.LEDGER_LOCK}, hashtext(%s))", ("one",)),
    ]


def test_a_write_to_one_ledger_runs_while_another_ledger_is_written(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3", domain=server.core)
    repo.import_document(
        "timing",
        server.core.normalize(
            {
                **new_ledger.build_doc({"title": "Timing"}),
                "_meta": {"rev": 1, "stamps": {}, "events": [], "members": {}},
            }
        ),
    )
    other = server.core.LOCK.ledger("other")
    other.acquire()
    try:
        assert runs(lambda: repo.apply_ops("timing"), patience=PATIENCE)[0]
    finally:
        other.release()
    with server.core.LOCK:
        blocked, thread = runs(lambda: repo.apply_ops("timing"))
        assert not blocked
    thread.join(PATIENCE)


def test_a_fresh_lock_reports_its_holds_exactly():
    lock = LedgerLock()
    assert lock.whole is False
    assert runs(lambda: lock.ledger("one").acquire() is True or pytest.fail("ledger acquire answered falsy"))[0]
    assert lock.held["one"] == 1
    lock.ledger("one").release()
    assert lock.acquire() is True
    assert lock.whole is True
    waiter, thread = runs(lock.acquire)
    assert not waiter
    assert lock.waiting == 1
    lock.release()
    thread.join(PATIENCE)
    assert lock.waiting == 0
    lock.release()
    assert lock.whole is False
    with pytest.raises(RuntimeError, match="^release unlocked lock$"):
        lock.release()


def test_a_write_waits_for_its_own_ledger_and_locks_the_store_by_slug(tmp_path, monkeypatch):
    repo = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3", domain=server.core)
    repo.import_document(
        "timing",
        server.core.normalize(
            {
                **new_ledger.build_doc({"title": "Timing"}),
                "_meta": {"rev": 1, "stamps": {}, "events": [], "members": {}},
            }
        ),
    )
    begun, begin = [], request_metrics.begin_ledger
    monkeypatch.setattr(
        request_metrics,
        "begin_ledger",
        lambda connection, slug, statement: begun.append((slug, statement)) or begin(connection, slug, statement),
    )
    own = server.core.LOCK.ledger("timing")
    own.acquire()
    try:
        blocked, thread = runs(lambda: repo.apply_ops("timing"))
        assert not blocked
    finally:
        own.release()
    thread.join(PATIENCE)
    assert begun == [("timing", BEGIN_IMMEDIATE)]
