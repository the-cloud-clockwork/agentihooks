import json
import os
import subprocess

import fakeredis
import pytest

from hooks import config
from scripts.doctor import rates_read
from scripts.doctor.rates import Pull, Window
from scripts.swarm.store import RedisStore

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
        return subprocess.CompletedProcess(argv, self.code, json.dumps(self.payload), "")


def test_pull_reads_state_merge_time_lines_and_files():
    assert rates_read.pull(RAW) == Pull("MERGED", rates_read.iso_ms("2026-10-06T10:00:00Z"), 10, ("a.py", "b/c.py"))
    assert rates_read.pull({"state": "OPEN", "additions": None}) == Pull("OPEN", None, 0, ())


def test_fetch_asks_gh_for_the_rate_fields_and_returns_none_on_failure():
    run = Run(RAW)
    assert rates_read.fetch(URL.format(1), run) == RAW
    assert run.calls == [["gh", "pr", "view", URL.format(1), "--json", rates_read.FIELDS]]
    assert rates_read.fetch(URL.format(1), Run(RAW, code=1)) is None

    def broken(*_, **__):
        raise subprocess.TimeoutExpired("gh", 20)

    assert rates_read.fetch(URL.format(1), broken) is None


def test_pulls_are_cached_settled_for_a_week_and_open_for_five_minutes():
    redis = fakeredis.FakeRedis(decode_responses=True)
    run = Run(RAW)
    first = rates_read.pulls(redis, "sw", [URL.format(1), URL.format(1)], run)
    again = rates_read.pulls(redis, "sw", [URL.format(1)], run)
    assert first == again == {URL.format(1): rates_read.pull(RAW)}
    assert len(run.calls) == 1
    key = f"agentihooks:swarm:sw:rates-pull:{URL.format(1)}"
    assert rates_read.OPEN_TTL_S < redis.ttl(key) <= rates_read.SETTLED_TTL_S
    rates_read.pulls(redis, "sw", [URL.format(2)], Run({**RAW, "state": "OPEN"}))
    assert 0 < redis.ttl(f"agentihooks:swarm:sw:rates-pull:{URL.format(2)}") <= rates_read.OPEN_TTL_S


def test_an_unreadable_pull_request_is_left_out_and_not_cached():
    redis = fakeredis.FakeRedis(decode_responses=True)
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
    (folder / "s1.jsonl").write_text(
        json.dumps({"at": "2026-10-06T10:00:00Z", "source": "e1"}) + "\n{torn\n" + json.dumps({"source": "no-time"})
    )
    old = folder / "s0.jsonl"
    old.write_text(json.dumps({"at": "2026-10-01T10:00:00Z", "source": "e0"}) + "\n")
    os.utime(old, (1, 1))
    rows = rates_read.injections(5_000)
    assert rows == [{"at": rates_read.iso_ms("2026-10-06T10:00:00Z"), "source": "e1"}]


def test_injections_without_a_folder_read_nothing(home):
    assert rates_read.injections(0) == []


class Ledger:
    def __init__(self, state):
        self._state = state

    def state(self, slug):
        return self._state


def test_load_reads_every_source_and_fetches_only_pull_requests_of_tasks_done_in_the_span(home, tmp_path):
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.redis.hset("agentihooks:swarm:sw:findings", "stale-claim/t1", json.dumps({"seen_at": 5, "measure": 40}))
    (home / "swarm-activity" / "sw").mkdir(parents=True)
    (home / "swarm-activity" / "sw" / "sw-eng-1.jsonl").write_text(json.dumps({"kind": "watch", "at": 3}) + "\n")
    (home / "injection_corrections.jsonl").write_text(json.dumps({"at": "2026-10-06T10:00:00Z", "source": "e1"}))
    gates = tmp_path / "swarm"
    rates_read.gate_log_path("sw", gates).parent.mkdir(parents=True)
    rates_read.gate_log_path("sw", gates).write_text(json.dumps({"gate": "talk", "kind": "deny", "at": 4}) + "\n")
    events = [
        {"at": 150 * MIN, "kind": "task done", "target": "tasks/t1"},
        {"at": 50 * MIN, "kind": "task done", "target": "tasks/t2"},
        {"at": 150 * MIN, "kind": "task done", "target": "tasks/t3"},
        {"at": 150 * MIN, "kind": "task done", "target": "phases/p1"},
        {"at": 150 * MIN, "kind": "task pr", "target": "tasks/t4"},
    ]
    tasks = [
        {"id": "t1", "pr_url": URL.format(1)},
        {"id": "t2", "pr_url": URL.format(2)},
        {"id": "t3", "pr_url": ""},
        {"id": "t4", "pr_url": URL.format(4)},
    ]
    run = Run(RAW)
    records = rates_read.load(
        store, Ledger({"tasks": tasks, "_meta": {"events": events}}), "sw", Window(100 * MIN, 200 * MIN), run, gates
    )
    assert [c[3] for c in run.calls] == [URL.format(1)]
    assert records.events == events
    assert sorted(records.tasks) == ["t1", "t2", "t3", "t4"]
    assert records.activity == {"sw-eng-1": [{"kind": "watch", "at": 3}]}
    assert records.findings == {"stale-claim/t1": {"seen_at": 5, "measure": 40}}
    assert records.gate_log == [{"gate": "talk", "kind": "deny", "at": 4}]
    assert records.injections == []
    assert records.corrections == [{"at": rates_read.iso_ms("2026-10-06T10:00:00Z"), "source": "e1"}]
    assert records.pulls == {URL.format(1): rates_read.pull(RAW)}


def test_a_missing_gate_log_and_ledger_meta_read_as_empty(home, tmp_path):
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    records = rates_read.load(store, Ledger({}), "sw", Window(0, 1), Run(RAW), tmp_path)
    assert (records.events, records.tasks, records.gate_log, records.pulls) == ([], {}, [], {})
