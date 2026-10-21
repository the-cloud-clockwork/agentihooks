import pytest

from scripts.ci_budget import defects, flakes
from scripts.swarm import ci_speed, grafana_alerts, langfuse, refresh_job
from scripts.swarm.store import RedisStore, SwarmConfig


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 2, 1))
    return store


@pytest.fixture
def called(monkeypatch):
    seen = []
    monkeypatch.setattr(ci_speed, "refresh", lambda *args: seen.append(("speed", args)) or [])
    monkeypatch.setattr(defects, "refresh", lambda *args: seen.append(("defects", args)) or ["updated"])
    monkeypatch.setattr(flakes, "refresh", lambda *args: seen.append(("flakes", args)) or ["filed"])
    monkeypatch.setattr(langfuse, "refresh", lambda *args: seen.append(("langfuse", args)) or [])
    monkeypatch.setattr(grafana_alerts, "refresh", lambda *args: seen.append(("grafana", args)) or ["fired"])
    return seen


def test_the_job_runs_every_refresh_with_its_own_arguments(store, called):
    ledger = object()
    assert refresh_job.run(store, "sw", ledger, 5) == ["updated", "filed", "fired"]
    config = store.config("sw")
    assert called == [
        ("speed", ("sw", config, store, 5)),
        ("defects", ("sw", config, store, 5)),
        ("flakes", ("sw", config, store, ledger, 5)),
        ("langfuse", (store, "sw", 5)),
        ("grafana", (store.redis, "sw", 5)),
    ]
    assert not store.redis.exists(refresh_job.lock_key(store, "sw"))


def test_a_failing_refresh_leaves_the_others_running(store, called, monkeypatch):
    def broken(*args):
        raise RuntimeError("gh is gone")

    monkeypatch.setattr(defects, "refresh", broken)
    assert refresh_job.run(store, "sw", object(), 5) == ["refresh failed: RuntimeError: gh is gone", "filed", "fired"]
    assert [name for name, _ in called] == ["speed", "flakes", "langfuse", "grafana"]
    assert not store.redis.exists(refresh_job.lock_key(store, "sw"))


def test_a_held_lock_runs_nothing(store, called):
    store.redis.set(refresh_job.lock_key(store, "sw"), "other")
    assert refresh_job.run(store, "sw", object(), 5) is None
    assert called == []
    assert store.redis.get(refresh_job.lock_key(store, "sw")) == "other"


def test_the_job_holds_its_lock_for_fifteen_minutes_while_it_runs(store, monkeypatch):
    seen = []
    monkeypatch.setattr(
        refresh_job,
        "refreshes",
        lambda store, slug, ledger, now: [
            lambda: seen.append(store.redis.pttl(refresh_job.lock_key(store, slug))) or []
        ],
    )
    refresh_job.run(store, "sw", object(), 5)
    assert 899_000 < seen[0] <= 900_000


def test_start_spawns_only_while_no_job_holds_the_lock(store, monkeypatch):
    spawned = []
    monkeypatch.setattr(refresh_job, "spawn", spawned.append)
    assert refresh_job.start(store, "sw") is True
    assert spawned == ["sw"]
    store.redis.set(refresh_job.lock_key(store, "sw"), "other")
    assert refresh_job.start(store, "sw") is False
    assert spawned == ["sw"]


def test_the_lock_key_sits_under_the_swarm(store):
    assert refresh_job.lock_key(store, "sw") == store.key("sw", "refresh-lock")
