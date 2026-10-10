import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.swarm import metrics_outbox

DAY_MS, Outbox, Settings, Table = (
    metrics_outbox.DAY_MS,
    metrics_outbox.Outbox,
    metrics_outbox.Settings,
    metrics_outbox.Table,
)

NOW = 1_800_000_000_000
SINK = Settings("http://clickhouse.test:8123", "writer", "pw")
TASKS = Table("task_moves", (("state", "String"), ("age_ms", "Int64"), ("share", "Float64")))


class Sink:
    def __init__(self):
        self.up = True
        self.queries = []
        self.rows = []

    def send(self, sink, query, body):
        self.queries.append(query)
        if not self.up:
            return False
        self.rows += [json.loads(line) for line in body.decode().splitlines()]
        return True


def row(event_id, ts_ms=NOW, **extra):
    base = {
        "event_id": event_id,
        "ledger": "sw",
        "ts_ms": ts_ms,
        "plan": "plan-a",
        "phase": "p1",
        "slice": "s1",
        "task": "t1",
        "state": "claimed",
        "age_ms": 5,
        "share": 0.5,
    }
    return base | extra


@pytest.fixture
def sink():
    return Sink()


@pytest.fixture
def box(tmp_path, sink):
    opened = Outbox(tmp_path / "outbox.sqlite", SINK, sink.send)
    yield opened
    opened.close()


def test_batch_rows_and_checkpoint_commit_together(box):
    box.db.execute("CREATE TABLE checkpoint (revision INTEGER)")
    first = row("first")
    second = row("second", ts_ms=NOW + 1)
    other = Table("other", TASKS.columns)
    box.append_many(
        [(TASKS, [first]), (other, [second])],
        lambda connection: connection.execute("INSERT INTO checkpoint VALUES (8)"),
    )
    assert box.recent(TASKS.name, NOW) == [first]
    assert box.recent("other", NOW) == [second]
    assert box.db.execute("SELECT revision FROM checkpoint").fetchall() == [(8,)]


def test_batch_rows_roll_back_when_checkpoint_fails(box):
    box.db.execute("CREATE TABLE checkpoint (revision INTEGER)")

    def failed(connection):
        connection.execute("INSERT INTO checkpoint VALUES (8)")
        raise OSError("checkpoint failed")

    with pytest.raises(OSError, match="checkpoint failed"):
        box.append_many([(TASKS, [row("first")]), (Table("other", TASKS.columns), [row("second")])], failed)
    assert box.recent(TASKS.name, NOW) == []
    assert box.recent("other", NOW) == []
    assert box.db.execute("SELECT revision FROM checkpoint").fetchall() == []


def test_a_malformed_batch_writes_no_rows_or_checkpoint(box):
    called = []
    with pytest.raises(ValueError):
        box.append_many(
            [(TASKS, [row("valid")]), (Table("other", TASKS.columns), [row("bad", ts_ms=False)])],
            lambda connection: called.append(connection),
        )
    assert box.recent(TASKS.name, NOW) == []
    assert called == []


def test_rows_written_while_the_sink_is_down_arrive_once_after_it_returns(box, sink):
    sink.up = False
    box.append(TASKS, [row("e1"), row("e2")])
    assert box.flush(NOW) == 0
    box.append(TASKS, [row("e3")])
    assert box.flush(NOW) == 0
    assert sink.rows == []
    sink.up = True
    assert box.flush(NOW) == 3
    assert sorted(r["event_id"] for r in sink.rows) == ["e1", "e2", "e3"]
    assert box.flush(NOW) == 0
    assert len(sink.rows) == 3


def test_a_down_sink_is_tried_once_per_flush(box, sink):
    sink.up = False
    box.append(TASKS, [row("e1")])
    box.append(Table("other"), [{k: row("e2")[k] for k in metrics_outbox.BASE_NAMES}])
    box.flush(NOW)
    assert len(sink.queries) == 1


def test_a_failed_insert_stops_the_flush_before_other_tables(box, sink):
    other = Table("other")
    box.append(TASKS, [row("e1")])
    box.append(other, [{k: row("e2")[k] for k in metrics_outbox.BASE_NAMES}])
    box.flush(NOW)
    sink.queries.clear()
    sink.up = False
    box.append(TASKS, [row("e3")])
    box.append(other, [{k: row("e4")[k] for k in metrics_outbox.BASE_NAMES}])
    assert box.flush(NOW) == 0
    assert len(sink.queries) == 1


def test_the_spool_waits_on_a_busy_lock(tmp_path, sink, monkeypatch):
    seen = []
    real = sqlite3.connect
    monkeypatch.setattr(metrics_outbox.sqlite3, "connect", lambda path, **kw: seen.append(kw) or real(path, **kw))
    Outbox(tmp_path / "o.sqlite", SINK, sink.send).close()
    assert seen == [{"timeout": metrics_outbox.LOCK_TIMEOUT_S}]
    assert metrics_outbox.LOCK_TIMEOUT_S == 10


def test_the_table_is_created_once_before_its_first_insert(box, sink):
    box.append(TASKS, [row("e1")])
    box.flush(NOW)
    box.append(TASKS, [row("e2")])
    box.flush(NOW)
    ddl, widen, insert, second = sink.queries
    assert widen == (
        "ALTER TABLE swarm.task_moves ADD COLUMN IF NOT EXISTS event_id String,"
        " ADD COLUMN IF NOT EXISTS ledger String, ADD COLUMN IF NOT EXISTS ts_ms Int64,"
        " ADD COLUMN IF NOT EXISTS plan String, ADD COLUMN IF NOT EXISTS phase String,"
        " ADD COLUMN IF NOT EXISTS slice String, ADD COLUMN IF NOT EXISTS task String,"
        " ADD COLUMN IF NOT EXISTS state String, ADD COLUMN IF NOT EXISTS age_ms Int64,"
        " ADD COLUMN IF NOT EXISTS share Float64"
    )
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS swarm.task_moves (")
    assert "ENGINE = ReplacingMergeTree" in ddl
    assert ddl.endswith("ORDER BY (ledger, event_id)")
    for column in ("event_id String", "ledger String", "ts_ms Int64", "plan String", "phase String"):
        assert column in ddl
    for column in ("slice String", "task String", "state String", "age_ms Int64", "share Float64"):
        assert column in ddl
    assert "ts DateTime64(3, 'UTC') DEFAULT fromUnixTimestamp64Milli(ts_ms, 'UTC')" in ddl
    assert insert == second == "INSERT INTO swarm.task_moves FORMAT JSONEachRow"


def test_a_failed_create_leaves_rows_waiting(box, sink):
    sink.up = False
    box.append(TASKS, [row("e1")])
    box.flush(NOW)
    sink.up = True
    box.flush(NOW)
    assert sink.queries[1].startswith("CREATE TABLE")
    assert [r["event_id"] for r in sink.rows] == ["e1"]


def test_a_replayed_event_id_is_spooled_once(box, sink):
    box.append(TASKS, [row("e1"), row("e1", state="done")])
    box.append(TASKS, [row("e1")])
    box.flush(NOW)
    box.append(TASKS, [row("e1")])
    box.flush(NOW)
    assert [r["event_id"] for r in sink.rows] == ["e1"]
    assert sink.rows[0]["state"] == "claimed"


def test_a_batch_whose_reply_was_lost_is_resent_under_the_same_event_ids(box, sink):
    box.append(TASKS, [row("e1"), row("e2")])
    box.flush(NOW)
    stored = list(sink.rows)
    lost = Sink()
    box.send = lambda settings, query, body: lost.send(settings, query, body) and False
    box.append(TASKS, [row("e3")])
    assert box.flush(NOW) == 0
    box.send = sink.send
    assert box.flush(NOW) == 1
    assert [r["event_id"] for r in lost.rows] == ["e3"]
    assert [r["event_id"] for r in sink.rows] == [*(r["event_id"] for r in stored), "e3"]
    assert sink.rows[-1] == lost.rows[0]


def test_the_create_is_remembered_across_outboxes(tmp_path, sink):
    first = Outbox(tmp_path / "o.sqlite", SINK, sink.send)
    first.append(TASKS, [row("e1")])
    first.flush(NOW)
    first.close()
    second = Outbox(tmp_path / "o.sqlite", SINK, sink.send)
    second.append(TASKS, [row("e2")])
    second.flush(NOW)
    second.close()
    assert [q.split(" ")[0] for q in sink.queries] == ["CREATE", "ALTER", "INSERT", "INSERT"]


def test_a_changed_table_is_created_again(tmp_path, sink):
    box = Outbox(tmp_path / "o.sqlite", SINK, sink.send)
    box.append(Table("grown"), [{k: row("e1")[k] for k in metrics_outbox.BASE_NAMES}])
    box.flush(NOW)
    box.append(
        Table("grown", (("state", "String"),)), [{k: row("e2")[k] for k in (*metrics_outbox.BASE_NAMES, "state")}]
    )
    box.flush(NOW)
    box.close()
    assert [q.split(" ")[0] for q in sink.queries] == ["CREATE", "ALTER", "INSERT", "CREATE", "ALTER", "INSERT"]
    assert "state String" in sink.queries[3]
    assert sink.queries[4].endswith("ADD COLUMN IF NOT EXISTS task String, ADD COLUMN IF NOT EXISTS state String")


def test_one_event_id_may_appear_in_two_tables(box, sink):
    box.append(TASKS, [row("e1")])
    box.append(Table("other"), [{k: row("e1")[k] for k in metrics_outbox.BASE_NAMES}])
    assert box.flush(NOW) == 2


def test_rows_ship_in_batches(box, sink, monkeypatch):
    monkeypatch.setattr(metrics_outbox, "BATCH", 2)
    box.append(TASKS, [row(f"e{n}") for n in range(5)])
    assert box.flush(NOW) == 5
    inserts = [q for q in sink.queries if q.startswith("INSERT")]
    assert len(inserts) == 3
    assert sorted(r["event_id"] for r in sink.rows) == [f"e{n}" for n in range(5)]


def test_a_failed_batch_keeps_the_rest_waiting(box, sink, monkeypatch):
    monkeypatch.setattr(metrics_outbox, "BATCH", 2)
    box.append(TASKS, [row(f"e{n}") for n in range(4)])
    calls = []

    def flaky(settings, query, body):
        calls.append(query)
        return len(calls) < 4 and sink.send(settings, query, body)

    box.send = flaky
    assert box.flush(NOW) == 2
    box.send = sink.send
    assert box.flush(NOW) == 2
    assert sorted(r["event_id"] for r in sink.rows) == [f"e{n}" for n in range(4)]


@pytest.mark.parametrize(
    "bad",
    [
        "not a row",
        {k: v for k, v in row("e1").items() if k != "task"},
        row("e1", extra="x"),
        row(""),
        row("e1", ledger=""),
        row("e1") | {"event_id": 7},
        row("e1", ts_ms="soon"),
        row("e1", ts_ms=True),
        row("e1", ts_ms=0),
        row("e1", age_ms=1.5),
        row("e1", age_ms=False),
        row("e1", age_ms=2**63),
        row("e1", age_ms=-(2**63) - 1),
        row("e1", share="half"),
        row("e1", share=float("nan")),
        row("e1", share=float("inf")),
        row("e1", plan=None),
    ],
)
def test_a_malformed_row_is_refused_at_append(box, sink, bad):
    with pytest.raises(ValueError):
        box.append(TASKS, [row("ok"), bad])
    box.flush(NOW)
    assert sink.rows == []


def test_whole_numbers_fill_a_float_column(box, sink):
    box.append(TASKS, [row("e1", share=1)])
    box.flush(NOW)
    assert sink.rows[0]["share"] == 1


def test_int64_bounds_are_accepted(box, sink):
    box.append(TASKS, [row("lo", age_ms=-(2**63)), row("hi", age_ms=2**63 - 1)])
    assert box.flush(NOW) == 2


def test_empty_node_path_parts_are_accepted(box, sink):
    box.append(TASKS, [row("e1", plan="", phase="", slice="", task="")])
    assert box.flush(NOW) == 1


@pytest.mark.parametrize(
    "name, columns",
    [
        ("Bad", ()),
        ("drop table", ()),
        ("1st", ()),
        ("", ()),
        ("ok", (("Bad", "String"),)),
        ("ok", (("x", "Array(String)"),)),
        ("ok", (("ledger", "String"),)),
        ("ok", (("ts", "Int64"),)),
        ("ok", (("x", "String"), ("x", "Int64"))),
    ],
)
def test_a_table_with_an_unsafe_name_or_column_is_refused(name, columns):
    with pytest.raises(ValueError):
        Table(name, columns)


def test_shipped_rows_older_than_a_day_are_pruned_and_waiting_rows_are_kept(box, sink):
    box.append(TASKS, [row("old", ts_ms=NOW - DAY_MS - 1), row("edge", ts_ms=NOW - DAY_MS), row("new")])
    box.flush(NOW)
    sink.up = False
    box.append(TASKS, [row("stuck", ts_ms=NOW - DAY_MS - 5)])
    box.flush(NOW)
    assert [r["event_id"] for r in box.recent("task_moves", NOW)] == ["edge", "new"]
    kept = sqlite3.connect(box.path).execute("SELECT event_id FROM spool ORDER BY event_id").fetchall()
    assert kept == [("edge",), ("new",), ("stuck",)]


def test_recent_reads_one_table_in_time_order_while_the_sink_is_down(box, sink):
    sink.up = False
    box.append(TASKS, [row("b", ts_ms=NOW - 10), row("a", ts_ms=NOW - 20)])
    box.append(Table("other"), [{k: row("c")[k] for k in metrics_outbox.BASE_NAMES}])
    box.flush(NOW)
    assert [r["event_id"] for r in box.recent("task_moves", NOW)] == ["a", "b"]
    assert box.recent("task_moves", NOW)[0] == row("a", ts_ms=NOW - 20)
    assert box.recent("missing", NOW) == []


def test_the_spool_runs_in_wal_mode(box):
    assert sqlite3.connect(box.path).execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_the_spool_folder_is_created(tmp_path, sink):
    path = tmp_path / "a" / "b" / "outbox.sqlite"
    Outbox(path, SINK, sink.send).close()
    assert path.exists()


def test_the_spool_lives_under_the_swarm_home(monkeypatch, tmp_path):
    monkeypatch.setattr(metrics_outbox.Path, "home", lambda: tmp_path)
    assert metrics_outbox.spool_path() == tmp_path / ".agentihooks" / "swarm" / "metrics-outbox.sqlite"


def test_settings_are_off_without_a_url():
    assert metrics_outbox.settings({}) is None
    assert metrics_outbox.settings({"AGENTIHOOKS_METRICS_URL": ""}) is None
    assert metrics_outbox.settings({"AGENTIHOOKS_METRICS_USER": "writer"}) is None


def test_settings_read_url_user_and_password():
    env = {
        "AGENTIHOOKS_METRICS_URL": "http://ch:8123/",
        "AGENTIHOOKS_METRICS_USER": "writer",
        "AGENTIHOOKS_METRICS_PASSWORD": "pw",
    }
    assert metrics_outbox.settings(env) == Settings("http://ch:8123", "writer", "pw")
    assert metrics_outbox.settings({**env, "AGENTIHOOKS_METRICS_PASSWORD": ""}) == Settings(
        "http://ch:8123", "writer", ""
    )


def test_settings_strip_only_trailing_slashes():
    env = {"AGENTIHOOKS_METRICS_URL": "http://ch:8123/sinkX//", "AGENTIHOOKS_METRICS_USER": "writer"}
    assert metrics_outbox.settings(env).url == "http://ch:8123/sinkX"


def test_settings_are_off_without_a_user():
    assert metrics_outbox.settings({"AGENTIHOOKS_METRICS_URL": "http://ch:8123"}) is None
    assert (
        metrics_outbox.settings({"AGENTIHOOKS_METRICS_URL": "http://ch:8123", "AGENTIHOOKS_METRICS_USER": ""}) is None
    )


class Recorder(BaseHTTPRequestHandler):
    status = 200
    seen = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        Recorder.seen.append(
            (
                parse_qs(urlsplit(self.path).query),
                self.headers.get("X-ClickHouse-User"),
                self.headers.get("X-ClickHouse-Key"),
                self.rfile.read(length),
            )
        )
        self.send_response(Recorder.status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Recorder.seen = []
    Recorder.status = 200
    httpd = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def test_post_sends_the_query_body_and_credentials(server):
    settings = Settings(f"http://127.0.0.1:{server.server_port}", "writer", "pw")
    assert metrics_outbox.post(settings, "INSERT INTO swarm.t FORMAT JSONEachRow", b'{"a":1}') is True
    query, user, key, body = Recorder.seen[0]
    assert query == {"query": ["INSERT INTO swarm.t FORMAT JSONEachRow"]}
    assert (user, key, body) == ("writer", "pw", b'{"a":1}')


def test_post_gives_up_after_its_timeout(monkeypatch):
    seen = []

    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        metrics_outbox.urllib.request, "urlopen", lambda request, **kw: seen.append((request, kw)) or Reply()
    )
    assert metrics_outbox.post(Settings("http://ch:8123", "w", "k"), "SELECT 1", b"") is True
    request, kw = seen[0]
    assert kw == {"timeout": metrics_outbox.TIMEOUT_S}
    assert metrics_outbox.TIMEOUT_S == 3
    assert request.get_method() == "POST"


def test_post_reports_a_refused_insert(server):
    Recorder.status = 500
    settings = Settings(f"http://127.0.0.1:{server.server_port}", "writer", "pw")
    assert metrics_outbox.post(settings, "SELECT 1", b"") is False


@pytest.mark.parametrize("url", ["ch:8123", "http://127.0.0.1:port", "http://127.0.0.1:1\n"])
def test_post_reports_a_malformed_url(url):
    assert metrics_outbox.post(Settings(url, "w", ""), "SELECT 1", b"") is False


def test_post_reports_an_unreachable_sink():
    port = HTTPServer(("127.0.0.1", 0), Recorder)
    closed = port.server_port
    port.server_close()
    assert metrics_outbox.post(Settings(f"http://127.0.0.1:{closed}", "w", ""), "SELECT 1", b"") is False


def test_outbox_flushes_through_a_real_http_sink(tmp_path, server):
    settings = Settings(f"http://127.0.0.1:{server.server_port}", "writer", "pw")
    box = Outbox(tmp_path / "o.sqlite", settings)
    box.append(TASKS, [row("e1")])
    assert box.flush(NOW) == 1
    box.close()
    assert [json.loads(seen[3]) for seen in Recorder.seen[2:]] == [row("e1")]
