import os
import threading
from contextlib import closing

import psycopg2
import pytest

from scripts.swarm_ledger.repository import postgres, sqlite

URL = os.environ.get("LEDGER_TEST_POSTGRES_URL", "")
PATIENCE = 45

pytestmark = pytest.mark.skipif(not URL, reason="the Ledger writer lock workflow sets LEDGER_TEST_POSTGRES_URL")


def idle_holders():
    with closing(psycopg2.connect(URL)) as watcher, watcher.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction'"
            " AND query LIKE 'SELECT pg_advisory_xact_lock%'"
        )
        return cursor.fetchone()[0]


def test_the_server_ends_an_idle_writer_lock_holder_and_the_waiting_writer_commits():
    holder = postgres.Connection(URL)
    holder.execute(sqlite.BEGIN_IMMEDIATE)
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
        assert idle_holders() == 0
    finally:
        holder.close()
        thread.join(PATIENCE)
