import json
import re
import runpy
import subprocess
import sys
import threading

import pytest

from scripts.swarm import cli, snapshot, snapshot_job
from scripts.swarm import store as swarm_store
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger, FakeRuntime

MINUTE = 60_000
SNAPSHOT_SPAWN = True


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def env(monkeypatch, store):
    ledger = FakeLedger([{"id": "t1", "lane": "eng"}])
    ledger.say = lambda slug, text, by=None: None
    rt = FakeRuntime()
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: rt)
    monkeypatch.setattr(cli.timer, "ensure", lambda binary: True)
    monkeypatch.setattr(snapshot, "worktrees", lambda repo, names, run=None: {})
    return store, ledger, rt


def _no_git(argv, **kw):
    return subprocess.CompletedProcess(argv, 128, "", "not a git repository")


def _running(store, slug="sw"):
    store.create(SwarmConfig(slug, "/repo", 2, 1))
    return store.update(slug, state="running")


def test_the_tick_returns_while_the_snapshot_runs_and_the_snapshot_still_completes(env, monkeypatch, capsys):
    store, ledger, rt = env
    gate, jobs, take = threading.Event(), [], snapshot.take

    def slow_take(*args, **kwargs):
        assert gate.wait(10)
        return take(*args, **kwargs)

    def spawn(slug):
        jobs.append(threading.Thread(target=snapshot_job.run, args=(store, slug, 1_000, {}, _no_git)))
        jobs[-1].start()

    monkeypatch.setattr(snapshot, "take", slow_take)
    monkeypatch.setattr(snapshot_job, "spawn", spawn)
    cli.main(["sw", "create", "--repo", "/repo"])
    assert cli.main(["sw", "start"]) == 0
    assert "started automatic snapshot" in capsys.readouterr().out.splitlines()
    assert len(jobs) == 1
    assert jobs[0].is_alive()
    assert snapshot.automatic("sw") == []
    gate.set()
    jobs[0].join(10)
    assert [p.name for p in snapshot.automatic("sw")] == ["auto-1000.json"]
    assert json.loads(snapshot.automatic("sw")[0].read_text())["taken_at"] == 1_000
    assert not store.redis.exists(snapshot_job.lock_key(store, "sw"))


def test_start_launches_a_job_only_when_a_snapshot_is_due_and_no_job_holds_the_lock(store, monkeypatch):
    _running(store)
    launched = []
    monkeypatch.setattr(snapshot_job, "spawn", launched.append)
    store.redis.set(snapshot_job.lock_key(store, "sw"), "other")
    assert snapshot_job.start(store, "sw", 0, {}) is False
    store.redis.delete(snapshot_job.lock_key(store, "sw"))
    assert snapshot_job.start(store, "sw", 0, {}) is True
    assert launched == ["sw"]
    store.update("sw", state="paused")
    assert snapshot_job.start(store, "sw", 0, {}) is False
    assert launched == ["sw"]


def test_start_waits_out_the_interval_after_the_last_automatic_snapshot(store, monkeypatch):
    _running(store)
    launched = []
    monkeypatch.setattr(snapshot_job, "spawn", launched.append)
    snapshot_job.run(store, "sw", 10 * MINUTE, {}, _no_git)
    assert snapshot_job.start(store, "sw", 40 * MINUTE - 1, {}) is False
    assert snapshot_job.start(store, "sw", 40 * MINUTE, {}) is True
    assert launched == ["sw"]


def test_the_lock_is_the_swarms_own_snapshot_lock_key(store):
    assert snapshot_job.lock_key(store, "sw") == store.key("sw", "snapshot-lock")


def test_the_job_reads_worktrees_through_the_runner_it_is_given(store):
    _running(store)
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return _no_git(argv, **kw)

    snapshot_job.run(store, "sw", 0, {}, runner)
    assert calls == [["git", "-C", "/repo", "worktree", "list", "--porcelain"]]


def test_a_second_job_takes_nothing_while_another_holds_the_lock(store):
    _running(store)
    key = snapshot_job.lock_key(store, "sw")
    store.redis.set(key, "other")
    assert snapshot_job.run(store, "sw", 0, {}, _no_git) is None
    assert snapshot.automatic("sw") == []
    assert store.redis.get(key) == "other"


def test_the_job_holds_its_lock_through_the_snapshot_and_releases_it_after(store, monkeypatch):
    _running(store)
    key, seen, take = snapshot_job.lock_key(store, "sw"), [], snapshot.take

    def watched(*args, **kwargs):
        seen.append((store.redis.pttl(key), store.redis.get(key)))
        return take(*args, **kwargs)

    monkeypatch.setattr(snapshot, "take", watched)
    assert snapshot_job.run(store, "sw", 5, {}, _no_git) == snapshot.auto_dir("sw") / "auto-5.json"
    snapshot_job.run(store, "sw", 30 * MINUTE + 5, {}, _no_git)
    [(ttl, token), (_, second)] = seen
    assert 29 * MINUTE < ttl <= 30 * MINUTE
    assert re.fullmatch(r"[0-9a-f]{32}", token)
    assert second != token
    assert not store.redis.exists(key)


def test_a_lock_another_job_takes_while_this_one_releases_stays_with_it(store, monkeypatch):
    _running(store)
    key, pipeline = snapshot_job.lock_key(store, "sw"), store.redis.pipeline

    class Racing:
        def __init__(self, *args, **kwargs):
            self.pipe = pipeline(*args, **kwargs)

        def __enter__(self):
            self.pipe.__enter__()
            return self

        def __exit__(self, *exc):
            return self.pipe.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self.pipe, name)

        def get(self, name):
            value = self.pipe.get(name)
            if name == key:
                store.redis.set(key, "other")
            return value

    monkeypatch.setattr(store.redis, "pipeline", Racing)
    snapshot_job.run(store, "sw", 0, {}, _no_git)
    assert store.redis.get(key) == "other"


def test_a_job_that_is_not_due_takes_no_snapshot_and_releases_its_lock(store):
    _running(store)
    snapshot_job.run(store, "sw", 0, {}, _no_git)
    assert snapshot_job.run(store, "sw", MINUTE, {}, _no_git) is None
    assert [p.name for p in snapshot.automatic("sw")] == ["auto-0.json"]
    assert not store.redis.exists(snapshot_job.lock_key(store, "sw"))


def test_a_failed_snapshot_still_releases_the_lock(store, monkeypatch):
    _running(store)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(snapshot, "take", broken)
    with pytest.raises(OSError):
        snapshot_job.run(store, "sw", 0, {}, _no_git)
    assert not store.redis.exists(snapshot_job.lock_key(store, "sw"))


def test_a_job_leaves_a_lock_another_job_took_after_its_own_expired(store, monkeypatch):
    _running(store)
    key, take = snapshot_job.lock_key(store, "sw"), snapshot.take

    def overtaken(*args, **kwargs):
        store.redis.set(key, "other")
        return take(*args, **kwargs)

    monkeypatch.setattr(snapshot, "take", overtaken)
    snapshot_job.run(store, "sw", 0, {}, _no_git)
    assert store.redis.get(key) == "other"


def test_spawn_starts_the_job_detached_with_its_errors_in_the_swarm_folder(monkeypatch):
    calls = []
    monkeypatch.setattr(snapshot_job.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    snapshot_job.spawn("sw")
    snapshot_job.log_path("sw").write_bytes(b"earlier error\n")
    snapshot_job.spawn("sw")
    assert snapshot_job.log_path("sw").read_bytes() == b"earlier error\n"
    [_, (argv, kw)] = calls
    assert argv == [sys.executable, "-m", "scripts.swarm.snapshot_job", "sw"]
    assert kw["start_new_session"] is True
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["stdout"] is subprocess.DEVNULL
    assert kw["stderr"].name == str(snapshot.path("sw").parent / "snapshot-job.log")
    assert kw["stderr"].closed


def test_the_job_command_snapshots_the_swarm_it_names(store, monkeypatch):
    _running(store)
    monkeypatch.setattr(snapshot_job, "connect", lambda: store)
    monkeypatch.setattr(snapshot_job, "now_ms", lambda: 7)
    monkeypatch.setattr(snapshot, "worktrees", lambda repo, names, run=None: {})
    assert snapshot_job.main(["sw"]) == 0
    assert [p.name for p in snapshot.automatic("sw")] == ["auto-7.json"]


def test_the_job_module_run_as_a_program_snapshots_the_swarm_on_its_command_line(store, monkeypatch):
    _running(store)
    monkeypatch.setattr(swarm_store, "connect", lambda: store)
    monkeypatch.setattr(snapshot, "worktrees", lambda repo, names, run=None: {})
    monkeypatch.setattr(sys, "argv", ["snapshot_job", "sw"])
    with pytest.raises(SystemExit) as ended:
        runpy.run_module("scripts.swarm.snapshot_job", run_name="__main__")
    assert ended.value.code == 0
    assert len(snapshot.automatic("sw")) == 1


def test_now_ms_reads_the_wall_clock_in_milliseconds(monkeypatch):
    monkeypatch.setattr(snapshot_job.time, "time", lambda: 1.5)
    assert snapshot_job.now_ms() == 1500
