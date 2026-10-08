import json
import os
import subprocess

import pytest

from hooks import config
from scripts.doctor import rates_read
from scripts.doctor.rates import Pull, Window
from scripts.swarm.keyspace import ROOT as KEY_ROOT
from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.xdist_group("fakeredis")
MIN = 60_000
URL = "https://github.com/o/r/pull/{}"
RAW = {
    "state": "MERGED",
    "mergedAt": "2026-10-06T10:00:00Z",
    "additions": 7,
    "deletions": 3,
    "files": [{"path": "a.py"}, {"path": "b/c.py"}],
}


class Run:
    def __init__(self, payload, code=0):
        self.payload, self.code, self.calls = payload, code, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.kwargs = kwargs
        payload = self.payload(argv[3]) if callable(self.payload) else self.payload
        return subprocess.CompletedProcess(argv, self.code, json.dumps(payload), "")


def fake_redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def test_pull_reads_state_merge_time_lines_and_files():
    assert rates_read.pull(RAW) == Pull("MERGED", rates_read.iso_ms("2026-10-06T10:00:00Z"), 10, ("a.py", "b/c.py"))
    assert rates_read.pull({"state": "OPEN", "additions": None}) == Pull("OPEN", None, 0, ())


def test_fetch_asks_gh_for_the_rate_fields_and_returns_none_on_failure():
    run = Run(RAW)
    assert rates_read.fetch(URL.format(1), run) == RAW
    assert run.calls == [["gh", "pr", "view", URL.format(1), "--json", rates_read.FIELDS]]
    assert run.kwargs == {"capture_output": True, "text": True, "timeout": 20}
    assert rates_read.fetch(URL.format(1), Run(RAW, code=1)) is None

    def broken(*_, **__):
        raise subprocess.TimeoutExpired("gh", 20)

    assert rates_read.fetch(URL.format(1), broken) is None


def test_pulls_are_cached_settled_for_a_week_and_open_for_five_minutes():
    redis = fake_redis()
    run = Run(RAW)
    first = rates_read.pulls(redis, "sw", [URL.format(1), URL.format(1)], run)
    again = rates_read.pulls(redis, "sw", [URL.format(1)], run)
    assert first == again == {URL.format(1): rates_read.pull(RAW)}
    assert len(run.calls) == 1
    key = f"{KEY_ROOT}:swarm:sw:rates-pull:{URL.format(1)}"
    assert rates_read.OPEN_TTL_S < redis.ttl(key) <= rates_read.SETTLED_TTL_S
    rates_read.pulls(redis, "sw", [URL.format(2)], Run({**RAW, "state": "OPEN"}))
    assert 0 < redis.ttl(f"{KEY_ROOT}:swarm:sw:rates-pull:{URL.format(2)}") <= rates_read.OPEN_TTL_S


def test_an_unreadable_pull_request_is_left_out_and_not_cached():
    redis = fake_redis()
    assert rates_read.pulls(redis, "sw", [URL.format(3)], Run(RAW, code=1)) == {}
    assert rates_read.pulls(redis, "sw", [URL.format(3)], Run(["not", "a", "pull"])) == {}
    assert redis.keys("*") == []


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", tmp_path)
    return tmp_path


def test_injections_read_session_files_touched_since_the_window_and_time_each_row(home):
    folder = home / "injections"
    folder.mkdir()
    first, later = "2026-10-06T10:00:00Z", "2026-10-06T11:00:00Z"
    (folder / "s1.jsonl").write_text(
        json.dumps({"at": first, "source": "e1"})
        + "\n{torn\n"
        + json.dumps({"source": "no-time"})
        + "\n"
        + json.dumps({"at": later, "source": "e2"})
    )
    (folder / "s2.jsonl").write_text(json.dumps({"at": later, "source": "e3"}))
    old = folder / "s0.jsonl"
    old.write_text(json.dumps({"at": "2026-10-01T10:00:00Z", "source": "e0"}) + "\n")
    os.utime(folder / "s1.jsonl", (100, 100))
    os.utime(folder / "s2.jsonl", (50, 50))
    os.utime(old, (49.96, 49.96))
    rows = rates_read.injections(50_000)
    assert rows == [
        {"at": rates_read.iso_ms(first), "source": "e1"},
        {"at": rates_read.iso_ms(later), "source": "e2"},
        {"at": rates_read.iso_ms(later), "source": "e3"},
    ]


def test_injections_without_a_folder_read_nothing(home):
    assert rates_read.injections(0) == []


class Ledger:
    def __init__(self, state):
        self._state, self.asked = state, []

    def state(self, slug):
        self.asked.append(slug)
        return self._state


def test_load_reads_every_source_and_fetches_only_pull_requests_of_tasks_done_in_the_span(home, tmp_path):
    store = RedisStore(fake_redis())
    store.redis.hset(f"{KEY_ROOT}:swarm:sw:findings", "stale-claim/t1", json.dumps({"seen_at": 5, "measure": 40}))
    (home / "swarm-activity" / "sw").mkdir(parents=True)
    (home / "swarm-activity" / "sw" / "sw-eng-1.jsonl").write_text(json.dumps({"kind": "watch", "at": 3}) + "\n")
    (home / "injection_corrections.jsonl").write_text(json.dumps({"at": "2026-10-06T10:00:00Z", "source": "e1"}))
    (home / "injections").mkdir()
    (home / "injections" / "s1.jsonl").write_text(json.dumps({"at": "2026-10-06T10:00:00Z", "source": "e1"}))
    gates = tmp_path / "swarm"
    assert rates_read.gate_log_path("sw", gates) == gates / "sw" / "gates" / "log.jsonl"
    rates_read.gate_log_path("sw", gates).parent.mkdir(parents=True)
    rates_read.gate_log_path("sw", gates).write_text(json.dumps({"gate": "talk", "kind": "deny", "at": 4}) + "\n")
    events = [
        {"at": 50 * MIN, "kind": "task done", "target": "tasks/t2"},
        {"at": 150 * MIN, "kind": "task done", "target": "tasks/t1"},
        {"at": 150 * MIN, "kind": "task done", "target": "tasks/t3"},
        {"at": 150 * MIN, "kind": "task done", "target": "tasks/gone"},
        {"at": 150 * MIN, "kind": "task done", "target": "phases/p1"},
        {"at": 150 * MIN, "kind": "task pr", "target": "tasks/t4"},
    ]
    tasks = [
        {"id": "t1", "pr_url": URL.format(1)},
        {"id": "t2", "pr_url": URL.format(2)},
        {"id": "t3", "pr_url": ""},
        {"id": "t4", "pr_url": URL.format(4)},
    ]
    run, ledger = Run(RAW), Ledger({"tasks": tasks, "_meta": {"events": events}})
    records = rates_read.load(store, ledger, "sw", Window(100 * MIN, 200 * MIN), run, gates)
    assert ledger.asked == ["sw"]
    assert [c[3] for c in run.calls] == [URL.format(1)]
    assert records.events == events
    assert sorted(records.tasks) == ["t1", "t2", "t3", "t4"]
    assert records.activity == {"sw-eng-1": [{"kind": "watch", "at": 3}]}
    assert records.findings == {"stale-claim/t1": {"seen_at": 5, "measure": 40}}
    assert records.gate_log == [{"gate": "talk", "kind": "deny", "at": 4}]
    assert records.injections == [{"at": rates_read.iso_ms("2026-10-06T10:00:00Z"), "source": "e1"}]
    assert records.corrections == [{"at": rates_read.iso_ms("2026-10-06T10:00:00Z"), "source": "e1"}]
    assert records.pulls == {URL.format(1): rates_read.pull(RAW)}


def test_repeated_rates_measurements_reuse_cached_verified_master_merge(home, tmp_path):
    from scripts.doctor import rates

    store = RedisStore(fake_redis())
    events = [{"at": 150 * MIN, "kind": "task done", "target": "tasks/t1", "by": "master@323133-0001"}]
    tasks = [{"id": "t1", "kind": "code", "pr_url": URL.format(1)}]
    run = Run(RAW)
    ledger = Ledger({"tasks": tasks, "_meta": {"events": events}})
    window = Window(100 * MIN, 200 * MIN)
    first = rates_read.load(store, ledger, "sw", window, run, tmp_path)
    second = rates_read.load(store, ledger, "sw", window, run, tmp_path)
    assert rates.ceremony(first, window)["outcomes"] == 1
    assert rates.ceremony(second, window) == rates.ceremony(first, window)
    assert len(run.calls) == 1


def test_lines_skip_a_torn_line_and_keep_the_rest(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"a": 1}\n{torn\n{"b": 2}\n')
    assert rates_read._lines(path) == [{"a": 1}, {"b": 2}]
    assert rates_read._lines(tmp_path / "missing.jsonl") == []


def test_an_unreadable_pull_request_does_not_stop_the_next_one():
    def payload(url):
        return RAW if url == URL.format(2) else ["not", "a", "pull"]

    found = rates_read.pulls(fake_redis(), "sw", [URL.format(2), URL.format(1)], Run(payload))
    assert found == {URL.format(2): rates_read.pull(RAW)}


def test_a_missing_gate_log_and_ledger_meta_read_as_empty(home, tmp_path):
    store = RedisStore(fake_redis())
    records = rates_read.load(store, Ledger({}), "sw", Window(0, 1), Run(RAW), tmp_path)
    assert (records.events, records.tasks, records.gate_log, records.pulls) == ([], {}, [], {})
