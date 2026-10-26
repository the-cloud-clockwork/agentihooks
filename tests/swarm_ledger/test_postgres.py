import sqlite3
from contextlib import contextmanager

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest
from psycopg2 import extensions

from scripts.swarm_ledger import storage
from scripts.swarm_ledger.repository import hierarchy, legacy, postgres, refresh, sqlite

ADDRESS = "postgresql://ledger@db.test/ledger"
IDLE = extensions.TRANSACTION_STATUS_IDLE
BUSY = extensions.TRANSACTION_STATUS_INTRANS


class Cursor:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, parameters):
        self.raw.log.append((sql, parameters))
        if self.raw.lost:
            self.raw.lost -= 1
            raise psycopg2.errors.LockNotAvailable("canceling statement due to lock timeout")
        if self.raw.fail is not None:
            raise self.raw.fail

    def fetchone(self):
        return None

    def __iter__(self):
        return iter(())

    def executemany(self, sql, rows):
        self.raw.log.append((sql, list(rows)))
        if self.raw.fail is not None:
            raise self.raw.fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Raw:
    def __init__(self):
        self.log, self.closed, self.status, self.fail = [], False, IDLE, None
        self.commits = self.rollbacks = self.lost = 0
        self.rollback_fails = None

    def cursor(self):
        return Cursor(self)

    def get_transaction_status(self):
        return self.status

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_fails is not None:
            raise self.rollback_fails

    def close(self):
        self.closed = True


@pytest.fixture
def opened(monkeypatch):
    made = []

    def connect(address, **settings):
        made.append(address)
        opened.settings.append(settings)
        raw = Raw()
        opened.raws.append(raw)
        return raw

    opened.raws, opened.settings = [], []
    opened.made = made
    monkeypatch.setattr(postgres, "POOLS", {})
    monkeypatch.setattr(psycopg2, "connect", connect)
    return opened


def test_the_url_setting_names_the_shared_database_and_empty_means_none():
    assert postgres.URL == "AGENTIHOOKS_SWARM_DATABASE_URL"
    assert postgres.url({postgres.URL: ADDRESS}) == ADDRESS
    assert postgres.url({postgres.URL: ""}) is None
    assert postgres.url({}) is None


def test_positional_statements_take_psycopg_placeholders_and_keep_literal_percent():
    assert postgres.translate("SELECT '%' FROM t WHERE a=? AND b=?", False) == "SELECT '%%' FROM t WHERE a=%s AND b=%s"


def test_named_statements_take_named_placeholders_and_keep_question_marks_and_casts():
    assert postgres.translate("SELECT x::text, '?' WHERE a=:slug AND b=:node", True) == (
        "SELECT x::text, '?' WHERE a=%(slug)s AND b=%(node)s"
    )


def test_the_sqlite_writer_lock_becomes_one_advisory_transaction_lock():
    assert postgres.translate(sqlite.BEGIN_IMMEDIATE, False) == "SELECT pg_advisory_xact_lock(1279607879)"


def test_catalog_reads_ask_the_information_schema_of_the_current_schema():
    tables = "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()"
    assert postgres.translate(sqlite.TABLES_SQL, False) == tables
    assert postgres.translate("SELECT 1 FROM sqlite_master WHERE name='hosted_fences'", False) == (
        f"{tables} AND table_name='hosted_fences'"
    )


def test_insert_or_ignore_becomes_insert_on_conflict_do_nothing():
    assert postgres.translate(hierarchy.INSERT_DEPENDENCY, False) == (
        "INSERT INTO work_dependencies VALUES (%s, %s, %s) ON CONFLICT DO NOTHING"
    )
    assert postgres.translate("SELECT 'INSERT OR IGNORE INTO '", False) == "SELECT 'INSERT OR IGNORE INTO '"


def test_hierarchy_reads_use_postgres_ordering_null_comparison_and_joins():
    order = (
        "((CASE n.kind WHEN 'plan' THEN 0 WHEN 'phase' THEN 1 WHEN 'slice' THEN 2 ELSE 3 END)::text"
        " || '.' || lpad(n.position::text, 9, '0'))"
    )
    root = (
        "(n.parent_id IS NOT DISTINCT FROM %(node)s OR (%(node)s IS NULL AND "
        "n.parent_id NOT IN (SELECT node_id FROM work_nodes WHERE ledger_slug=%(slug)s)))"
    )
    assert postgres.translate(hierarchy.CHILDREN, True) == (
        "SELECT n.node_id, n.kind, n.parent_id, CASE WHEN %(node)s IS NULL THEN 0 ELSE 1 END FROM work_nodes n "
        f"WHERE n.ledger_slug=%(slug)s AND {root} ORDER BY {order}"
    )
    assert (
        postgres.translate(hierarchy.SUBTREE, True)
        == f"""
WITH RECURSIVE tree(node_id, kind, parent_id, depth, sort) AS (
  SELECT n.node_id, n.kind, n.parent_id, 0, {order} FROM work_nodes n
  WHERE n.ledger_slug=%(slug)s AND (n.node_id=%(node)s OR (%(node)s IS NULL AND {root}))
  UNION ALL
  SELECT n.node_id, n.kind, n.parent_id, tree.depth + 1, tree.sort || '/' || {order}
  FROM tree JOIN work_nodes n ON n.ledger_slug=%(slug)s AND n.parent_id=tree.node_id WHERE tree.depth < 3
)
SELECT node_id, kind, parent_id, depth FROM tree ORDER BY sort
"""
    )
    assert (
        postgres.translate(hierarchy.ANCESTORS, True)
        == """
WITH RECURSIVE up(node_id, kind, parent_id, depth) AS (
  SELECT node_id, kind, parent_id, 0 FROM work_nodes WHERE ledger_slug=%(slug)s AND node_id=%(node)s
  UNION ALL
  SELECT n.node_id, n.kind, n.parent_id, up.depth + 1
  FROM work_nodes n JOIN up ON n.ledger_slug=%(slug)s AND n.node_id=up.parent_id WHERE up.depth < 3
)
SELECT node_id, kind, parent_id, depth FROM up WHERE depth > 0 ORDER BY depth DESC
"""
    )
    assert (
        postgres.translate(hierarchy.DEPENDENTS, True)
        == f"""
WITH RECURSIVE down(node_id, depth) AS (
  SELECT node_id, 1 FROM work_dependencies WHERE ledger_slug=%(slug)s AND requires_id=%(node)s
  UNION
  SELECT d.node_id, down.depth + 1
  FROM work_dependencies d JOIN down ON d.ledger_slug=%(slug)s AND d.requires_id=down.node_id WHERE down.depth < 64
)
SELECT n.node_id, n.kind, n.parent_id, MIN(down.depth) AS nearest
FROM down JOIN work_nodes n ON n.ledger_slug=%(slug)s AND n.node_id=down.node_id WHERE n.node_id != %(node)s
GROUP BY n.node_id, n.kind, n.parent_id, n.position ORDER BY nearest, {order}
"""
    )


def test_the_schema_takes_64_bit_integers_and_byte_ordered_text_per_statement():
    script = "CREATE TABLE IF NOT EXISTS x (context TEXT, n INTEGER, texts TEXT, PRIMARY KEY(context));\n  \nCREATE INDEX i ON x(n);\n"
    assert postgres.ddl(script) == [
        'CREATE TABLE IF NOT EXISTS x (context TEXT COLLATE "C", n BIGINT, texts TEXT COLLATE "C", PRIMARY KEY(context))',
        "CREATE INDEX i ON x(n)",
    ]


def test_an_empty_answer_has_no_rows():
    empty = postgres.Empty()
    assert empty.fetchone() is None
    assert empty.fetchall() == []
    assert list(empty) == []


def test_postgres_failures_surface_as_the_sqlite_errors_callers_handle():
    with pytest.raises(sqlite3.IntegrityError, match="^duplicate$") as caught, postgres.mapped():
        raise psycopg2.IntegrityError("duplicate")
    assert isinstance(caught.value.__cause__, psycopg2.IntegrityError)
    with pytest.raises(sqlite3.OperationalError, match="^absent$") as caught, postgres.mapped():
        raise psycopg2.errors.UndefinedTable("absent")
    assert isinstance(caught.value.__cause__, psycopg2.errors.UndefinedTable)
    with pytest.raises(sqlite3.DatabaseError, match="^gone$") as caught, postgres.mapped():
        raise psycopg2.OperationalError("gone")
    assert type(caught.value) is sqlite3.DatabaseError
    assert isinstance(caught.value.__cause__, psycopg2.OperationalError)
    with pytest.raises(ValueError, match="^other$"), postgres.mapped():
        raise ValueError("other")


def test_a_pool_reuses_rolled_back_connections_and_keeps_at_most_eight(opened):
    pool = postgres.Pool(ADDRESS)
    taken = [pool.take() for _ in range(postgres.KEPT + 1)]
    assert opened.made == [ADDRESS] * 9
    for raw in taken:
        pool.give(raw)
    assert [raw.rollbacks for raw in taken] == [1] * 9
    assert pool.idle == taken[:8]
    assert [raw.closed for raw in taken] == [False] * 8 + [True]
    assert pool.take() is taken[7]
    assert opened.made == [ADDRESS] * 9


def test_a_pool_closes_a_connection_it_cannot_roll_back_or_that_closed(opened):
    pool = postgres.Pool(ADDRESS)
    broken, closed = pool.take(), pool.take()
    broken.rollback_fails = psycopg2.OperationalError("broken")
    closed.closed = True
    pool.give(broken)
    pool.give(closed)
    assert broken.closed and pool.idle == []


def test_a_pool_that_cannot_connect_raises_the_sqlite_error(monkeypatch):
    def refuse(address, **settings):
        raise psycopg2.OperationalError("refused")

    monkeypatch.setattr(psycopg2, "connect", refuse)
    with pytest.raises(sqlite3.DatabaseError, match="^refused$"):
        postgres.Pool(ADDRESS).take()


def test_a_new_connection_bounds_lock_waits_idle_transactions_and_statements_and_keeps_alive(opened):
    postgres.Pool(ADDRESS).take()
    assert opened.settings == [
        {
            "options": "-c lock_timeout=10s -c idle_in_transaction_session_timeout=15s -c statement_timeout=60s"
            " -c agentihooks.ledger_writer_version=1",
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
    ]


def test_a_writer_that_loses_its_lock_wait_rolls_back_and_asks_again(opened):
    connection = postgres.Connection(ADDRESS)
    raw = opened.raws[0]
    raw.lost = 2
    cursor = connection.execute(sqlite.BEGIN_IMMEDIATE)
    assert isinstance(cursor, Cursor) and cursor.raw is raw
    assert raw.log == [("SELECT pg_advisory_xact_lock(1279607879)", ())] * 3
    assert raw.rollbacks == 2


def test_a_writer_never_granted_the_lock_fails_with_the_named_reason(opened):
    connection = postgres.Connection(ADDRESS)
    raw = opened.raws[0]
    raw.lost = 4
    with pytest.raises(
        sqlite3.OperationalError, match="^the ledger writer lock was not granted in 3 waits of 10 seconds$"
    ):
        connection.execute(sqlite.BEGIN_IMMEDIATE)
    assert len(raw.log) == 3 and raw.rollbacks == 3 and raw.lost == 1


def test_the_writer_lock_is_refused_inside_an_open_transaction_so_no_retry_discards_its_work(opened):
    connection = postgres.Connection(ADDRESS)
    raw = opened.raws[0]
    raw.status = BUSY
    with pytest.raises(sqlite3.OperationalError, match="^cannot start a transaction within a transaction$"):
        connection.execute(sqlite.BEGIN_IMMEDIATE)
    assert raw.log == [] and raw.rollbacks == 0


def test_a_writer_lock_failure_other_than_a_lost_wait_is_mapped_at_once(opened):
    connection = postgres.Connection(ADDRESS)
    raw = opened.raws[0]
    raw.fail = psycopg2.OperationalError("server closed the connection unexpectedly")
    with pytest.raises(sqlite3.DatabaseError, match="^server closed the connection unexpectedly$"):
        connection.execute(sqlite.BEGIN_IMMEDIATE)
    assert len(raw.log) == 1 and raw.rollbacks == 0


def test_one_pool_per_address(opened):
    assert postgres.pool(ADDRESS) is postgres.pool(ADDRESS)
    assert postgres.pool(ADDRESS) is not postgres.pool(ADDRESS + "2")
    assert postgres.pool(ADDRESS).address == ADDRESS


def test_a_connection_translates_statements_and_passes_their_parameters(opened):
    connection = postgres.Connection(ADDRESS)
    raw = opened.raws[0]
    cursor = connection.execute("SELECT 1 FROM t WHERE a=?", ("x",))
    assert isinstance(cursor, Cursor)
    connection.execute("SELECT 1 FROM t WHERE a=:slug", {"slug": "x"})
    connection.execute("SELECT 1")
    assert raw.log == [
        ("SELECT 1 FROM t WHERE a=%s", ("x",)),
        ("SELECT 1 FROM t WHERE a=%(slug)s", {"slug": "x"}),
        ("SELECT 1", ()),
    ]


def test_pragmas_answer_nothing_and_hosted_fences_are_refused(opened):
    connection = postgres.Connection(ADDRESS)
    assert isinstance(connection.execute("PRAGMA foreign_keys=ON"), postgres.Empty)
    with pytest.raises(sqlite3.OperationalError, match="^hosted fences belong to SQLite ledger folders$"):
        connection.execute("PRAGMA table_info(fields)")
    assert opened.raws[0].log == []


def test_begin_opens_a_repeatable_read_snapshot_only_as_the_first_statement(opened):
    connection = postgres.Connection(ADDRESS)
    raw = opened.raws[0]
    assert isinstance(connection.execute(sqlite.BEGIN), Cursor)
    raw.status = BUSY
    with pytest.raises(sqlite3.OperationalError, match="^cannot start a transaction within a transaction$"):
        connection.execute(sqlite.BEGIN)
    assert raw.log == [("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", ())]
    raw.get_transaction_status = lambda: (_ for _ in ()).throw(psycopg2.InterfaceError("closed"))
    with pytest.raises(sqlite3.DatabaseError, match="^closed$"):
        connection.execute(sqlite.BEGIN)


def test_connection_failures_and_batches_are_mapped_and_translated(opened, monkeypatch):
    def batch(cursor, sql, rows):
        cursor.executemany(sql, rows)

    monkeypatch.setattr(psycopg2.extras, "execute_batch", batch)
    connection = postgres.Connection(ADDRESS)
    raw = opened.raws[0]
    connection.executemany(hierarchy.INSERT_DEPENDENCY, iter([("s", "a", "b")]))
    assert raw.log == [("INSERT INTO work_dependencies VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", [("s", "a", "b")])]
    raw.fail = psycopg2.IntegrityError("taken")
    with pytest.raises(sqlite3.IntegrityError, match="^taken$"):
        connection.execute("SELECT 1")
    with pytest.raises(sqlite3.IntegrityError, match="^taken$"):
        connection.executemany("SELECT ?", [(1,)])
    raw.cursor = lambda: (_ for _ in ()).throw(psycopg2.InterfaceError("cursor gone"))
    with pytest.raises(sqlite3.DatabaseError, match="^cursor gone$"):
        connection.execute("SELECT 1")


def test_a_script_runs_each_translated_statement(opened):
    connection = postgres.Connection(ADDRESS)
    connection.executescript("CREATE TABLE a (x TEXT);CREATE TABLE b (y INTEGER);")
    assert opened.raws[0].log == [('CREATE TABLE a (x TEXT COLLATE "C")', ()), ("CREATE TABLE b (y BIGINT)", ())]


def test_a_connection_block_commits_on_success_and_rolls_back_on_failure(opened):
    connection = postgres.Connection(ADDRESS)
    raw = opened.raws[0]
    with connection as entered:
        assert entered is connection
    assert (raw.commits, raw.rollbacks) == (1, 0)
    with pytest.raises(KeyError), connection:
        raise KeyError("x")
    assert (raw.commits, raw.rollbacks) == (1, 1)


def test_commit_and_rollback_failures_are_mapped(opened):
    connection = postgres.Connection(ADDRESS)
    raw = opened.raws[0]
    raw.commit = raw.rollback = lambda: (_ for _ in ()).throw(psycopg2.OperationalError("lost"))
    with pytest.raises(sqlite3.DatabaseError, match="^lost$"):
        connection.commit()
    with pytest.raises(sqlite3.DatabaseError, match="^lost$"):
        connection.rollback()


def test_closing_a_connection_returns_it_to_its_pool(opened):
    connection = postgres.Connection(ADDRESS)
    connection.close()
    assert postgres.pool(ADDRESS).idle == [opened.raws[0]]
    assert opened.raws[0].rollbacks == 1


def test_the_database_folder_opens_a_pooled_connection_and_raises_when_unreachable(opened, monkeypatch):
    database = postgres.Database(ADDRESS)
    with database.read_only() as connection:
        assert isinstance(connection, postgres.Connection)
        assert connection.raw is opened.raws[0]
    assert postgres.pool(ADDRESS).idle == [opened.raws[0]]

    def refuse(address, **settings):
        raise psycopg2.OperationalError("refused")

    monkeypatch.setattr(postgres, "POOLS", {})
    monkeypatch.setattr(psycopg2, "connect", refuse)
    with pytest.raises(sqlite3.DatabaseError, match="^refused$"), database.read_only():
        pass


def test_sqlite_read_helpers_open_an_object_that_carries_its_own_reader():
    opened = []

    class Folder:
        @contextmanager
        def read_only(self):
            opened.append(True)
            yield "connection"

    with sqlite.read_only(Folder()) as connection:
        assert connection == "connection"
    assert opened == [True]


def test_the_repository_sets_its_schema_up_once_under_the_writer_lock(opened):
    repository = postgres.PostgresLedgerRepository(ADDRESS, domain="domain")
    assert repository.adopts is False and repository.address == ADDRESS
    assert repository._key("s") == (ADDRESS, "s")
    with repository.connect() as connection:
        assert isinstance(connection, postgres.Connection)
    raw = opened.raws[0]
    statements = [sql for sql, _ in raw.log]
    assert statements[0] == "SELECT pg_advisory_xact_lock(1279607879)"
    assert statements[1] == "SET LOCAL statement_timeout = 0"
    assert statements[2:] == [
        *postgres.ddl(
            sqlite.SCHEMA
            + postgres.OUTBOX_SCHEMA
            + hierarchy.SCHEMA
            + sqlite.ROWS_SCHEMA
            + refresh.SCHEMA
            + postgres.FLOOR_SCHEMA
        ),
        postgres.translate(postgres.RAISE_FLOOR),
        postgres.GUARD,
        postgres.GUARD_TRIGGER,
    ]
    assert raw.log[-3][1] == (postgres.REQUIRED,)
    assert raw.commits == 1
    assert repository._ready == {ADDRESS}
    with repository.connect():
        pass
    assert len(raw.log) == len(statements) and raw.commits == 1
    assert postgres.pool(ADDRESS).idle == [raw]


def test_a_repository_set_up_by_another_thread_is_not_set_up_again(opened):
    repository = postgres.PostgresLedgerRepository(ADDRESS)
    connection = postgres.Connection(ADDRESS)
    repository._ready.add(ADDRESS)
    repository._set_up(connection, ADDRESS)
    assert opened.raws[0].log == []


def test_a_repository_bound_to_another_domain_shares_its_caches_and_database():
    repository = postgres.PostgresLedgerRepository(ADDRESS, domain="first")
    assert repository.bound("first") is repository
    twin = repository.bound("second")
    assert isinstance(twin, postgres.PostgresLedgerRepository)
    assert (twin.address, twin.domain) == (ADDRESS, "second")
    assert (twin._cache, twin._loads, twin._ready, twin._guard) == (
        repository._cache,
        repository._loads,
        repository._ready,
        repository._guard,
    )
    assert twin._cache is repository._cache and twin._loads is repository._loads
    assert twin._ready is repository._ready and twin._guard is repository._guard


def test_a_snapshot_reads_one_repeatable_read_transaction(opened):
    repository = postgres.PostgresLedgerRepository(ADDRESS)
    repository._ready.add(ADDRESS)
    with repository.snapshot() as snapshot:
        assert isinstance(snapshot, sqlite.Snapshot)
        assert snapshot.repository is repository
        assert snapshot.connection.raw is opened.raws[0]
    assert opened.raws[0].log == [("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", ())]
    assert opened.raws[0].commits == 1


def test_events_since_reads_one_snapshot_and_relay_holds_take_the_writer_lock(opened):
    repository = postgres.PostgresLedgerRepository(ADDRESS)
    repository._ready.add(ADDRESS)
    with pytest.raises(sqlite.Missing):
        repository.events_since("s", 3)
    raw = opened.raws[0]
    assert raw.log == [
        ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", ()),
        ("SELECT 1 FROM ledgers WHERE slug=%s", ("s",)),
    ]
    lock = "SELECT pg_advisory_xact_lock(1279607879)"
    for write, arguments, statement in (
        (
            repository.hold_revision,
            ("inbox", "s", 4),
            "INSERT INTO relay_journal VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        ),
        (
            repository.release_revisions,
            ("inbox", "s", 1, 4),
            "DELETE FROM relay_journal WHERE kind=%s AND slug=%s AND revision BETWEEN %s AND %s",
        ),
        (repository.drop_revisions, ("inbox", "s"), "DELETE FROM relay_journal WHERE kind=%s AND slug=%s"),
    ):
        raw.log.clear()
        write(*arguments)
        assert raw.log == [(lock, ()), (statement, arguments)]


def test_reading_collection_ids_takes_one_snapshot_across_the_row_tables(opened):
    assert sqlite.read_ids(postgres.Database(ADDRESS), "s", "tasks") == ()
    raw = opened.raws[0]
    assert raw.log == [
        ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", ()),
        *[
            (f"SELECT position, path FROM {table} WHERE slug=%s AND parent=%s", ("s", '["tasks"]'))
            for table in sqlite.TABLES
        ],
    ]
    assert raw.commits == 1


def test_storage_builds_the_shared_repository_only_for_the_default_folder(monkeypatch, tmp_path):
    monkeypatch.setenv(postgres.URL, ADDRESS)
    shared = storage.build(domain="domain")
    assert isinstance(shared, postgres.PostgresLedgerRepository)
    assert (shared.address, shared.domain) == (ADDRESS, "domain")
    assert storage.shared() == ADDRESS
    assert type(storage.build(tmp_path / storage.DATABASE)) is sqlite.SQLiteLedgerRepository
    monkeypatch.delenv(postgres.URL)
    assert type(storage.build()) is sqlite.SQLiteLedgerRepository
    assert storage.shared() is None


def test_storage_read_helpers_read_the_shared_database_when_its_url_is_set(monkeypatch, tmp_path):
    seen = []
    for name in ("read_document", "read_ids", "read_ledger", "read_ledgers", "read_registry", "read_slugs", "fenced"):
        monkeypatch.setattr(sqlite, name, lambda *args, name=name: seen.append((name, *args)) or name)
    monkeypatch.delenv(postgres.URL, raising=False)
    assert storage.store(tmp_path) is tmp_path
    assert storage.read_ledger(tmp_path, "s", "a", "b") == "read_ledger"
    monkeypatch.setenv(postgres.URL, ADDRESS)
    assert isinstance(storage.store(tmp_path), postgres.Database)
    assert storage.store(tmp_path).address == ADDRESS
    assert storage.read_document(tmp_path, "s") == "read_document"
    assert storage.read_ids(tmp_path, "s", "tasks") == "read_ids"
    assert storage.read_ledger(tmp_path, "s", "a") == "read_ledger"
    assert storage.read_ledgers(tmp_path, "a", "b") == "read_ledgers"
    assert storage.read_registry(tmp_path, "bin") == "read_registry"
    assert storage.read_slugs(tmp_path) == "read_slugs"
    assert storage.fenced(tmp_path, "s") == "fenced"
    assert seen[0] == ("read_ledger", tmp_path, "s", "a", "b")
    assert [entry[0] for entry in seen[1:]] == [
        "read_document",
        "read_ids",
        "read_ledger",
        "read_ledgers",
        "read_registry",
        "read_slugs",
        "fenced",
    ]
    assert all(isinstance(entry[1], postgres.Database) for entry in seen[1:])
    assert [entry[2:] for entry in seen[1:]] == [("s",), ("s", "tasks"), ("s", "a"), ("a", "b"), ("bin",), (), ("s",)]


def test_a_shared_database_never_adopts_files_left_in_a_host_folder(tmp_path, monkeypatch):
    folder = tmp_path / "ledgers"
    folder.mkdir()
    (folder / "old.json").write_text("{}")
    for filename in legacy.REGISTRIES.values():
        (folder / filename).write_text("{}")
    touched = []
    monkeypatch.setattr(legacy, "storage_lock", lambda directory: touched.append(directory))
    repository = postgres.PostgresLedgerRepository(ADDRESS)
    monkeypatch.setattr(postgres.PostgresLedgerRepository, "directory", folder)
    legacy.adopt(repository)
    legacy.adopt_bin(repository)
    assert touched == []
    assert sorted(path.name for path in folder.iterdir()) == sorted(["old.json", *legacy.REGISTRIES.values()])


def test_the_controller_tasks_api_writes_the_shared_database_when_its_url_is_set(monkeypatch, tmp_path):
    from scripts.swarm_v2 import control_service

    built = []

    class Lease:
        held = True

        def __init__(self, folder):
            pass

        def acquire(self, owner):
            return self

    monkeypatch.setattr(control_service.ledger_writer, "WriterLease", Lease)
    monkeypatch.setattr(control_service.storage, "build", lambda path=None: built.append(path) or path)
    monkeypatch.setattr(control_service, "TasksAPI", lambda grants, tasks, ledger, held: ledger)
    service = control_service.ControlService.__new__(control_service.ControlService)
    service.grants = service.tasks = None
    monkeypatch.setenv(postgres.URL, ADDRESS)
    service.mount_ledger(tmp_path)
    monkeypatch.delenv(postgres.URL)
    service.mount_ledger(tmp_path)
    assert built == [None, tmp_path / storage.DATABASE]
    assert service.tasks_api == tmp_path / storage.DATABASE


def test_a_session_start_with_only_a_shared_database_starts_the_local_ledger_server(monkeypatch, tmp_path):
    from scripts.swarm_ledger import ledger_hook

    def closed(*args, **kwargs):
        raise OSError("closed")

    started = []
    monkeypatch.setattr(ledger_hook, "LEDGER_DIR", tmp_path)
    monkeypatch.delenv("LEDGER_AUTOSTART", raising=False)
    monkeypatch.delenv("SWARM_URL", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.setattr(ledger_hook.socket, "create_connection", closed)
    monkeypatch.setattr(ledger_hook.subprocess, "Popen", lambda command, **kwargs: started.append(command[1:]))
    monkeypatch.delenv(postgres.URL, raising=False)
    ledger_hook.serve_ledgers()
    assert started == []
    monkeypatch.setenv(postgres.URL, ADDRESS)
    ledger_hook.serve_ledgers()
    assert started == [[str(ledger_hook.HERE / "ledger_server.py"), "--ensure"]]


def test_a_stream_reads_through_a_named_server_cursor_two_thousand_rows_at_a_time(monkeypatch):
    made = []

    class Named:
        def __init__(self, name):
            self.name, self.ran, self.fetches = name, None, []
            self.batches = [[(1,), (2,)], [(3,)], []]

        def execute(self, sql, parameters):
            self.ran = (sql, parameters)

        def fetchmany(self, size):
            self.fetches.append(size)
            return self.batches.pop(0)

    class Streaming(Raw):
        def cursor(self, name=None):
            made.append(Named(name))
            return made[-1]

    monkeypatch.setattr(postgres, "POOLS", {})
    monkeypatch.setattr(psycopg2, "connect", lambda address, **settings: Streaming())
    connection = postgres.Connection(ADDRESS)
    assert list(connection.stream("SELECT path FROM fields WHERE slug=?", ("s",))) == [(1,), (2,), (3,)]
    assert list(connection.stream("SELECT 1")) == [(1,), (2,), (3,)]
    first, second = made
    assert first.fetches == second.fetches == [2000, 2000, 2000]
    assert first.ran == ("SELECT path FROM fields WHERE slug=%s", ("s",))
    assert second.ran == ("SELECT 1", ())
    assert first.name.startswith("stream_") and second.name.startswith("stream_") and first.name != second.name
