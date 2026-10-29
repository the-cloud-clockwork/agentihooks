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
    done, answers = threading.Event(), []

    def run():
        answers.append(target())
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    finished = done.wait(patience)
    return finished and answers[0], thread


def until(condition):
    seen = threading.Event()
    for _ in range(int(PATIENCE / 0.01)):
        if condition():
            return True
        seen.wait(0.01)
    return False


def test_a_fresh_lock_is_free():
    lock = LedgerLock()
    assert lock.whole is False
    assert lock.waiting == 0
    assert lock.held == set()
    assert not lock.locked()


def test_two_ledgers_hold_at_once_and_one_ledger_holds_alone():
    lock = LedgerLock()
    assert runs(lock.ledger("one").acquire)[0] is True
    assert lock.locked()
    assert runs(lock.ledger("two").acquire)[0] is True
    assert lock.held == {"one", "two"}
    lock.ledger("two").release()
    same, thread = runs(lock.ledger("one").acquire)
    assert same is False
    lock.ledger("one").release()
    thread.join(PATIENCE)
    assert lock.held == {"one"}
    lock.ledger("one").release()
    assert not lock.locked()


def test_the_whole_lock_waits_for_ledgers_and_blocks_them():
    lock = LedgerLock()
    assert runs(lock.ledger("one").acquire)[0] is True
    first, thread = runs(lock.acquire)
    assert first is False
    second, other = runs(lock.acquire)
    assert second is False
    assert lock.waiting == 2
    assert lock.acquire(blocking=False) is False
    queued, behind = runs(lock.ledger("two").acquire)
    assert queued is False
    lock.ledger("one").release()
    assert until(lambda: lock.waiting == 1)
    assert lock.whole is True
    lock.release()
    assert until(lambda: lock.waiting == 0)
    assert lock.whole is True
    assert lock.held == set()
    lock.release()
    assert until(lambda: lock.held == {"two"})
    assert lock.whole is False
    for waiter in (thread, other, behind):
        waiter.join(PATIENCE)


def test_a_free_whole_lock_answers_true_and_refuses_a_second_release():
    lock = LedgerLock()
    assert runs(lock.acquire)[0] is True
    assert runs(lambda: lock.acquire(blocking=False))[0] is False
    lock.release()
    assert lock.whole is False
    with pytest.raises(RuntimeError, match="^release unlocked lock$"):
        lock.release()


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


@pytest.fixture
def repo(tmp_path):
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
    return repo


def test_a_write_locks_only_its_own_ledger(repo, monkeypatch):
    taken, begun, begin = [], [], request_metrics.begin_ledger

    class Recording:
        def ledger(self, slug):
            taken.append(slug)
            return threading.Lock()

    monkeypatch.setattr(repo.domain, "LOCK", Recording())
    monkeypatch.setattr(
        request_metrics,
        "begin_ledger",
        lambda connection, slug, statement: begun.append((slug, statement)) or begin(connection, slug, statement),
    )
    repo.apply_ops("timing")
    assert taken == ["timing"]
    assert begun == [("timing", BEGIN_IMMEDIATE)]


def test_a_write_runs_beside_another_ledger_and_waits_for_the_whole_store(repo):
    lock = server.core.LOCK
    assert runs(lock.ledger("other").acquire)[0] is True
    try:
        assert runs(lambda: repo.apply_ops("timing") and True, patience=PATIENCE)[0] is True
    finally:
        lock.ledger("other").release()
    assert runs(lock.acquire)[0] is True
    blocked, thread = runs(lambda: repo.apply_ops("timing"))
    assert blocked is False
    lock.release()
    thread.join(PATIENCE)
