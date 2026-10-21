import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci_budget import defects, flakes
from scripts.swarm import (
    ci_speed,
    commands,
    grafana_alerts,
    langfuse,
    lease,
    ledger_client,
    queue_priority,
    refresh_job,
)
from scripts.swarm.store import RedisStore, SwarmConfig

REFRESH_SPAWN = True


class Ledger:
    def state(self, slug):
        return {"tasks": [{"id": f"{slug}-t1"}]}


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 2, 1))
    monkeypatch.setattr(commands, "hive_id", lambda: "home")
    assert lease.acquire(store, "sw", "home") is not None
    return store


@pytest.fixture
def called(monkeypatch):
    seen = []
    monkeypatch.setattr(ci_speed, "refresh", lambda *args: seen.append(("speed", args)) or [])
    monkeypatch.setattr(defects, "refresh", lambda *args: seen.append(("defects", args)) or ["updated"])
    monkeypatch.setattr(flakes, "refresh", lambda *args: seen.append(("flakes", args)) or ["filed"])
    monkeypatch.setattr(langfuse, "refresh", lambda *args: seen.append(("langfuse", args)) or [])
    monkeypatch.setattr(grafana_alerts, "refresh", lambda *args: seen.append(("grafana", args)) or ["fired"])
    monkeypatch.setattr(queue_priority, "refresh", lambda *args: seen.append(("queue", args)) or ["applied"])
    return seen


def test_the_job_runs_every_refresh_with_its_own_arguments(store, called):
    ledger = Ledger()
    assert refresh_job.run(store, "sw", ledger, 5) == ["applied", "updated", "filed", "fired"]
    config = store.config("sw")
    assert called == [
        ("queue", (store, "sw", [{"id": "sw-t1"}], ledger)),
        ("speed", ("sw", config, store, 5)),
        ("defects", ("sw", config, store, 5)),
        ("flakes", ("sw", config, store, ledger, 5)),
        ("langfuse", (store, "sw", 5)),
        ("grafana", (store.redis, "sw", 5)),
    ]
    assert not store.redis.exists(refresh_job.lock_key(store, "sw"))


def test_a_failing_refresh_ends_the_job_and_frees_its_lock(store, called, monkeypatch):
    def broken(*args):
        raise RuntimeError("gh is gone")

    monkeypatch.setattr(defects, "refresh", broken)
    with pytest.raises(RuntimeError, match="^gh is gone$"):
        refresh_job.run(store, "sw", Ledger(), 5)
    assert [name for name, _ in called] == ["queue", "speed"]
    assert not store.redis.exists(refresh_job.lock_key(store, "sw"))


def test_a_held_lock_runs_nothing(store, called):
    store.redis.set(refresh_job.lock_key(store, "sw"), "other")
    assert refresh_job.run(store, "sw", Ledger(), 5) is None
    assert called == []
    assert store.redis.get(refresh_job.lock_key(store, "sw")) == "other"


def test_a_hive_without_the_swarm_lease_runs_nothing(store, called, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "elsewhere")
    assert refresh_job.run(store, "sw", Ledger(), 5) is None
    assert called == []
    assert not store.redis.exists(refresh_job.lock_key(store, "sw"))


def test_a_swarm_with_no_lease_runs_nothing(store, called):
    store.redis.delete(store.key("sw", "control-owner"))
    assert refresh_job.run(store, "sw", Ledger(), 5) is None
    assert called == []


def test_the_job_takes_its_lock_for_fifteen_minutes(store, monkeypatch):
    taken = []
    real = store.redis.set

    def recording(key, value, **kwargs):
        taken.append((key, kwargs))
        return real(key, value, **kwargs)

    monkeypatch.setattr(store.redis, "set", recording)
    monkeypatch.setattr(refresh_job, "refreshes", lambda *args: [])
    assert refresh_job.run(store, "sw", Ledger(), 5) == []
    assert taken == [(refresh_job.lock_key(store, "sw"), {"nx": True, "px": 900_000})]


def test_start_spawns_only_while_no_job_holds_the_lock(store, monkeypatch):
    spawned = []
    monkeypatch.setattr(refresh_job, "spawn", spawned.append)
    assert refresh_job.start(store, "sw") is True
    assert spawned == ["sw"]
    store.redis.set(refresh_job.lock_key(store, "sw"), "other")
    assert refresh_job.start(store, "sw") is False
    assert spawned == ["sw"]


def test_start_spawns_at_most_once_every_five_minutes(store, monkeypatch):
    spawned = []
    monkeypatch.setattr(refresh_job, "spawn", spawned.append)
    assert refresh_job.start(store, "sw") is True
    assert refresh_job.start(store, "sw") is False
    assert spawned == ["sw"]
    assert store.redis.get(refresh_job.started_key(store, "sw")) == "1"
    assert 299_000 <= store.redis.pttl(refresh_job.started_key(store, "sw")) <= 300_000
    store.redis.delete(refresh_job.started_key(store, "sw"))
    assert refresh_job.start(store, "sw") is True
    assert spawned == ["sw", "sw"]


def test_the_started_key_sits_under_the_swarm(store):
    assert refresh_job.started_key(store, "sw") == store.key("sw", "refresh-started")


def test_the_lock_key_sits_under_the_swarm(store):
    assert refresh_job.lock_key(store, "sw") == store.key("sw", "refresh-lock")


def test_the_job_log_sits_in_the_swarm_folder():
    assert refresh_job.log_path("sw") == Path.home() / ".agentihooks" / "swarm" / "sw" / "refresh-job.log"


def test_spawn_detaches_the_job_with_its_output_in_the_log(monkeypatch):
    launched = []

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        assert kwargs["stdout"] is kwargs["stderr"] and kwargs["stderr"].name == str(refresh_job.log_path("sw"))
        assert kwargs["stderr"].mode == "ab"

    monkeypatch.setattr(subprocess, "Popen", popen)
    refresh_job.spawn("sw")
    refresh_job.spawn("sw")
    [(argv, kwargs), again] = launched
    assert again[0] == argv
    assert argv == [sys.executable, "-m", "scripts.swarm.refresh_job", "sw"]
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["start_new_session"] is True
    assert set(kwargs) == {"stdin", "stdout", "stderr", "start_new_session"}
    assert refresh_job.log_path("sw").parent.is_dir()


def test_now_ms_is_the_wall_clock_in_milliseconds(monkeypatch):
    monkeypatch.setattr(refresh_job.time, "time", lambda: 12.3456)
    assert refresh_job.now_ms() == 12345


def test_main_runs_the_job_for_its_swarm_and_prints_each_action(store, monkeypatch, capsys):
    ledger, seen = Ledger(), []
    monkeypatch.setattr(refresh_job, "connect", lambda: store)
    monkeypatch.setattr(ledger_client, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(refresh_job, "now_ms", lambda: 7)
    monkeypatch.setattr(refresh_job, "run", lambda *args: seen.append(args) or ["one", "two"])
    assert refresh_job.main(["sw"]) == 0
    assert seen == [(store, "sw", ledger, 7)]
    assert capsys.readouterr().out == "sw: one\nsw: two\n"


def test_main_prints_nothing_when_another_job_holds_the_lock(store, monkeypatch, capsys):
    monkeypatch.setattr(refresh_job, "connect", lambda: store)
    monkeypatch.setattr(ledger_client, "LedgerClient", lambda: Ledger())
    monkeypatch.setattr(refresh_job, "run", lambda *args: None)
    assert refresh_job.main(["sw"]) == 0
    assert capsys.readouterr().out == ""
