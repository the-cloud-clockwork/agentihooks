"""The wait pass collects hive launch acknowledgements apart from the tick pass, under the shared acks lock."""

import os
from typing import Any

import pytest

from scripts.swarm import ack_pass, commands, controller, lease, ledger_client
from scripts.swarm import tick as engine
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

DOC = {"tasks": [{"id": "t1", "state": "claimed"}]}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    import fakeredis

    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    monkeypatch.delenv("LEDGER_URL", raising=False)
    monkeypatch.delenv("SWARM_URL", raising=False)
    made = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    made.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return made


@pytest.fixture
def collected(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def collect(*args: Any) -> list[str]:
        calls.append(args)
        assert store.redis.get(store.key("sw", ack_pass.LOCK))
        return ["spawned engineer@a1b2c3-0001 for t1"]

    monkeypatch.setattr(engine, "_collect_acks", collect)
    monkeypatch.setattr(ledger_client, "pass_state", lambda ledger, slug: calls.append(("read", ledger, slug)) or DOC)
    monkeypatch.setattr(controller, "FencedRuntime", lambda *args: ("fenced", *args))
    return calls


def _launch(store: RedisStore, state: str) -> AgentRecord:
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "t1", started_at=1_000)
    store.record_launch("sw", agent, state)
    return agent


@pytest.mark.parametrize("state", ["acknowledging", "launching"])
def test_a_waiting_launch_is_collected_by_the_wait_pass_under_the_acks_lock(
    store: RedisStore, collected: list[Any], state: str, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    import time

    _launch(store, state)
    before = int(time.time() * 1000)
    assert ack_pass.run(store, "sw", ledger="ledger", runtime="runtime") == ["spawned engineer@a1b2c3-0001 for t1"]
    after = int(time.time() * 1000)
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert [(e["slug"], e["phase"]) for e in events if e["step"].endswith(".collect")] == [
        ("sw", "started"),
        ("sw", "finished"),
    ]
    held = lease.current(store, "sw")
    assert held is not None and held.owner == commands.hive_id()
    read, call = collected
    assert read == ("read", "ledger", "sw")
    slug, config, passed, ledger, fenced, doc, now_ms = call
    assert (slug, config, passed, ledger, doc) == ("sw", store.config("sw"), store, "ledger", DOC)
    assert fenced == ("fenced", store, "sw", held, "runtime", controller.deployment_for(config, os.environ))
    assert before <= now_ms <= after
    assert store.redis.get(store.key("sw", ack_pass.LOCK)) is None


def test_a_workstation_launch_job_makes_the_wait_pass_collect(store: RedisStore, collected: list[Any]) -> None:
    from scripts.swarm import launch_job

    store.redis.hset(store.key("sw", launch_job.KEY), "engineer@a1b2c3-0001", "{}")
    assert ack_pass.run(store, "sw", ledger="ledger", runtime="runtime") == ["spawned engineer@a1b2c3-0001 for t1"]
    assert len(collected) == 2


@pytest.mark.parametrize("state", ["started", "waiting"])
def test_no_waiting_launch_reads_no_ledger(store: RedisStore, collected: list[Any], state: str) -> None:
    _launch(store, state)
    assert ack_pass.run(store, "sw", ledger="ledger", runtime="runtime") == []
    assert collected == []


def test_a_held_acks_lock_leaves_the_collection_to_its_holder(store: RedisStore, collected: list[Any]) -> None:
    _launch(store, "acknowledging")
    store.redis.set(store.key("sw", ack_pass.LOCK), "tick")
    assert ack_pass.run(store, "sw", ledger="ledger", runtime="runtime") == []
    assert collected == []
    assert store.redis.get(store.key("sw", ack_pass.LOCK)) == "tick"


def test_a_foreign_hive_lease_leaves_the_acknowledgements_alone(store: RedisStore, collected: list[Any]) -> None:
    _launch(store, "acknowledging")
    lease.acquire(store, "sw", "another-controller")
    assert ack_pass.run(store, "sw", ledger="ledger", runtime="runtime") == []
    assert collected == []


@pytest.mark.parametrize("authority", [None, "trusted-tick-fixture"])
def test_a_remote_ledger_swarm_is_collected_only_with_tick_authority(
    store: RedisStore, collected: list[Any], monkeypatch: pytest.MonkeyPatch, authority: str | None
) -> None:
    from scripts.swarm_ledger import authority_context

    _launch(store, "acknowledging")
    store.redis.hset(store.key("sw", "config"), "api_url", "https://hosted.example")
    acquired: list[str] = []
    held = lease.Lease(commands.hive_id(), 1, 10**15)
    monkeypatch.setattr(lease, "acquire", lambda own, slug, owner: acquired.append(slug) or held)
    monkeypatch.setattr(lease, "require", lambda own, slug, current: None)
    from scripts.swarm_ledger import ledger_link

    asked: list[Any] = []
    remote, headers = ledger_link.remote, authority_context.tick_headers
    monkeypatch.setattr(ledger_link, "remote", lambda slug: asked.append(("remote", slug)) or remote(slug=slug))
    monkeypatch.setattr(
        authority_context, "tick_headers", lambda slug: asked.append(("headers", slug)) or headers(slug)
    )
    with authority_context.bound(authority):
        found = ack_pass.run(store, "sw", ledger="ledger", runtime="runtime")
    assert {kind for kind, _ in asked} == {"remote", "headers"}
    assert {slug for _, slug in asked} == {"sw"}
    assert acquired == (["sw"] if authority else [])
    assert found == (["spawned engineer@a1b2c3-0001 for t1"] if authority else [])


def test_the_default_runtime_routes_a_deferring_herdr_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import runtime
    from scripts.swarm_v2.runtime import routed

    monkeypatch.setattr(runtime, "HerdrRuntime", lambda: "herdr")
    monkeypatch.setattr(runtime, "deferring", lambda inner: ("deferring", inner))
    monkeypatch.setattr(routed, "routed", lambda herdr: ("routed", herdr))
    assert ack_pass._runtime() == ("routed", ("deferring", "herdr"))


def test_a_stopped_swarm_collects_nothing(store: RedisStore, collected: list[Any]) -> None:
    _launch(store, "acknowledging")
    store.update("sw", state="stopped")
    assert ack_pass.run(store, "sw", ledger="ledger", runtime="runtime") == []
    assert collected == []


def test_a_holder_renews_its_lock_every_ten_seconds_and_never_a_successors(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.scheduler import process_interval

    made: list[Any] = []

    class Interval:
        def __init__(self, callback: Any, *, every: float) -> None:
            self.callback, self.every, self.closed = callback, every, False
            made.append(self)

        def start(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(process_interval, "Interval", Interval)
    key = store.key("sw", ack_pass.LOCK)
    with ack_pass.collecting(store, "sw"):
        (renewing,) = made
        assert renewing.every == 10.0
        store.redis.pexpire(key, 5_000)
        renewing.callback()
        assert 29_000 < store.redis.pttl(key) <= 30_000
        store.redis.set(key, "successor", px=5_000)
        renewing.callback()
        assert store.redis.pttl(key) <= 5_000
    assert renewing.closed is True
    assert store.redis.get(key) == "successor"
    with ack_pass.collecting(store, "sw") as taken:
        assert taken is False
    assert len(made) == 1


def test_a_redis_error_while_renewing_is_reported_and_the_next_beat_retries(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    key = store.key("sw", ack_pass.LOCK)
    store.redis.set(key, "mine", px=5_000)
    real = store.redis.pexpire
    monkeypatch.setattr(store.redis, "pexpire", lambda *args: (_ for _ in ()).throw(RedisConnectionError("down")))
    ack_pass._renew(store, key, "mine")
    assert capsys.readouterr().err == f"renewing the acks lock {key} failed: ConnectionError\n"
    monkeypatch.setattr(store.redis, "pexpire", real)
    ack_pass._renew(store, key, "mine")
    assert 29_000 < store.redis.pttl(key) <= 30_000


def test_the_lock_is_released_even_when_the_renewal_will_not_stop(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.scheduler import process_interval

    class Interval:
        def __init__(self, callback: Any, *, every: float) -> None:
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            raise RuntimeError("scheduled interval did not stop")

    monkeypatch.setattr(process_interval, "Interval", Interval)
    with pytest.raises(RuntimeError, match="^scheduled interval did not stop$"):
        with ack_pass.collecting(store, "sw"):
            pass
    assert store.redis.get(store.key("sw", ack_pass.LOCK)) is None


def test_the_lock_is_released_only_by_its_own_holder(store: RedisStore) -> None:
    key = store.key("sw", ack_pass.LOCK)
    with ack_pass.collecting(store, "sw") as taken:
        assert taken is True
        assert 29_000 < store.redis.pttl(key) <= 30_000
        with ack_pass.collecting(store, "sw") as second:
            assert second is False
        assert store.redis.get(key)
        store.redis.set(key, "successor")
    assert store.redis.get(key) == "successor"
    store.redis.delete(key)
    with ack_pass.collecting(store, "sw") as taken:
        assert taken is True
    assert store.redis.get(key) is None
