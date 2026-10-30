import os
import threading
import uuid
from contextlib import closing

import psycopg2
import pytest

from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.repository import postgres, sqlite
from tests.swarm_ledger.test_hierarchy_reads import STATE

URL = os.environ.get("LEDGER_TEST_POSTGRES_URL", "")
PATIENCE = 30

pytestmark = pytest.mark.skipif(not URL, reason="the Ledger writer lock workflow sets LEDGER_TEST_POSTGRES_URL")


def alive(pid):
    with closing(psycopg2.connect(URL)) as watcher, watcher.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,))
        return cursor.fetchone()[0]


def test_the_server_ends_an_idle_writer_lock_holder_and_the_waiting_writer_commits():
    holder = postgres.Connection(URL)
    holder.execute(sqlite.BEGIN_IMMEDIATE)
    pid = holder.raw.get_backend_pid()
    written, failed = threading.Event(), []

    def write():
        try:
            with postgres.Connection(URL) as writer:
                writer.execute(sqlite.BEGIN_IMMEDIATE)
                writer.execute("SELECT 1")
            written.set()
        except Exception as exc:
            failed.append(repr(exc))

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    try:
        assert written.wait(PATIENCE), (
            failed or f"the second writer still waited after {PATIENCE} s behind an idle holder"
        )
        assert alive(pid) == 0
    finally:
        holder.close()
        thread.join(PATIENCE)


def holds(first_lock, second_lock, patience=5.0):
    first = postgres.Connection(URL)
    got = threading.Event()

    def second():
        with postgres.Connection(URL) as writer:
            second_lock(writer)
            got.set()

    try:
        first_lock(first)
        thread = threading.Thread(target=second, daemon=True)
        thread.start()
        granted = got.wait(patience)
    finally:
        first.close()
    thread.join(PATIENCE)
    return granted


def test_writers_of_two_ledgers_hold_their_locks_at_once():
    assert holds(lambda c: c.lock_ledger("one"), lambda c: c.lock_ledger("two"))


def test_a_second_writer_of_the_same_ledger_waits():
    assert not holds(lambda c: c.lock_ledger("one"), lambda c: c.lock_ledger("one"), patience=2.0)


def test_a_whole_store_writer_and_a_ledger_writer_exclude_each_other():
    assert not holds(lambda c: c.execute(sqlite.BEGIN_IMMEDIATE), lambda c: c.lock_ledger("one"), patience=2.0)
    assert not holds(lambda c: c.lock_ledger("one"), lambda c: c.execute(sqlite.BEGIN_IMMEDIATE), patience=2.0)


def test_a_cold_command_reads_credentials_while_the_writer_lock_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    slug = f"credentials-{uuid.uuid4().hex[:12]}"
    repository = postgres.PostgresLedgerRepository(URL)
    repository.import_document(slug, STATE)
    token = repository.token(slug)
    cold = postgres.PostgresLedgerRepository(URL)
    monkeypatch.setattr(cold, "_ready", set())
    monkeypatch.setattr(ledger, "repository", cold)
    monkeypatch.setattr(postgres, "LOCK_TRIES", 1)
    with repository.connect() as holder, holder:
        holder.execute(sqlite.BEGIN_IMMEDIATE)
        holder.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
        assert ledger.local_credentials(slug, "", True) == {"X-Ledger-Token": token}
        assert ledger.local_credentials(f"{slug}-missing", "", True) == {"X-Ledger-Token": ""}
