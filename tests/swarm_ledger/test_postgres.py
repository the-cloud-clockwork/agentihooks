import sqlite3
from contextlib import contextmanager

import psycopg2
import pytest
from psycopg2 import extensions

from scripts.swarm_ledger import storage
from scripts.swarm_ledger.repository import hierarchy, legacy, postgres, sqlite

ADDRESS = "postgresql://ledger@db.test/ledger"
IDLE = extensions.TRANSACTION_STATUS_IDLE
BUSY = extensions.TRANSACTION_STATUS_INTRANS


class Cursor:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, parameters):
        self.raw.log.append((sql, parameters))
        if self.raw.fail is not None:
            raise self.raw.fail

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
        self.commits = self.rollbacks = 0
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

    def connect(address):
        made.append(address)
        raw = Raw()
        opened.raws.append(raw)
        return raw

    opened.raws = []
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
    children = postgres.translate(hierarchy.CHILDREN, True)
    assert children == (
        "SELECT n.node_id, n.kind, n.parent_id, CASE WHEN %(node)s IS NULL THEN 0 ELSE 1 END FROM work_nodes n "
        "WHERE n.ledger_slug=%(slug)s AND (n.parent_id IS NOT DISTINCT FROM %(node)s OR (%(node)s IS NULL AND "
        "n.parent_id NOT IN (SELECT node_id FROM work_nodes WHERE ledger_slug=%(slug)s))) "
        f"ORDER BY {order}"
    )
    subtree = postgres.translate(hierarchy.SUBTREE, True)
    assert "CROSS JOIN" not in subtree
    assert "FROM tree JOIN work_nodes n ON n.ledger_slug=%(slug)s AND n.parent_id=tree.node_id" in subtree
    dependents = postgres.translate(hierarchy.DEPENDENTS, True)
    assert f"GROUP BY n.node_id, n.kind, n.parent_id, n.position ORDER BY nearest, {order}" in dependents
    for read in hierarchy.READS.values():
        assert "printf" not in postgres.translate(read, True)


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
    with pytest.raises(sqlite3.OperationalError, match="^gone$") as caught, postgres.mapped():
        raise psycopg2.OperationalError("gone")
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
    def refuse(address):
        raise psycopg2.OperationalError("refused")

    monkeypatch.setattr(psycopg2, "connect", refuse)
    with pytest.raises(sqlite3.OperationalError, match="^refused$"):
        postgres.Pool(ADDRESS).take()


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


def test_pragmas_answer_nothing_without_reaching_the_database(opened):
    connection = postgres.Connection(ADDRESS)
    assert isinstance(connection.execute("PRAGMA foreign_keys=ON"), postgres.Empty)
    assert opened.raws[0].log == []


def test_begin_opens_a_repeatable_read_snapshot_only_as_the_first_statement(opened):
    connection = postgres.Connection(ADDRESS)
    raw = opened.raws[0]
    assert isinstance(connection.execute(sqlite.BEGIN), Cursor)
    raw.status = BUSY
    assert isinstance(connection.execute(sqlite.BEGIN), postgres.Empty)
    assert raw.log == [("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", ())]


def test_connection_failures_and_batches_are_mapped_and_translated(opened):
    connection = postgres.Connection(ADDRESS)
    raw = opened.raws[0]
    connection.executemany(hierarchy.INSERT_DEPENDENCY, [("s", "a", "b")])
    assert raw.log == [("INSERT INTO work_dependencies VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", [("s", "a", "b")])]
    raw.fail = psycopg2.IntegrityError("taken")
    with pytest.raises(sqlite3.IntegrityError, match="^taken$"):
        connection.execute("SELECT 1")
    with pytest.raises(sqlite3.IntegrityError, match="^taken$"):
        connection.executemany("SELECT ?", [(1,)])


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
    with pytest.raises(sqlite3.OperationalError, match="^lost$"):
        connection.commit()
    with pytest.raises(sqlite3.OperationalError, match="^lost$"):
        connection.rollback()


def test_closing_a_connection_returns_it_to_its_pool(opened):
    connection = postgres.Connection(ADDRESS)
    connection.close()
    assert postgres.pool(ADDRESS).idle == [opened.raws[0]]
    assert opened.raws[0].rollbacks == 1


def test_the_database_folder_opens_a_pooled_connection_or_none_when_unreachable(opened, monkeypatch):
    database = postgres.Database(ADDRESS)
    with database.read_only() as connection:
        assert isinstance(connection, postgres.Connection)
        assert connection.raw is opened.raws[0]
    assert postgres.pool(ADDRESS).idle == [opened.raws[0]]

    def refuse(address):
        raise psycopg2.OperationalError("refused")

    monkeypatch.setattr(postgres, "POOLS", {})
    monkeypatch.setattr(psycopg2, "connect", refuse)
    with database.read_only() as connection:
        assert connection is None


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
    assert statements[1:] == postgres.ddl(sqlite.SCHEMA + hierarchy.SCHEMA + sqlite.ROWS_SCHEMA)
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
    (tmp_path / "old.json").write_text("{}")
    for filename in legacy.REGISTRIES.values():
        (tmp_path / filename).write_text("{}")
    touched = []
    monkeypatch.setattr(legacy, "storage_lock", lambda directory: touched.append(directory))
    repository = postgres.PostgresLedgerRepository(ADDRESS)
    monkeypatch.setattr(postgres.PostgresLedgerRepository, "directory", tmp_path)
    legacy.adopt(repository)
    legacy.adopt_bin(repository)
    assert touched == []
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(["old.json", *legacy.REGISTRIES.values()])
