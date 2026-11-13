"""One hook process reaches Redis through one short timeout client; gate shims share a few seconds of gate config."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.gates import modes
from scripts.swarm import hook_redis
from scripts.swarm.hook_redis import client as hook_client

ENV = {
    "AGENTIHOOKS_SWARM": "demo",
    "AGENTIHOOKS_AGENT_NAME": "bob",
    "AGENTIHOOKS_SWARM_REDIS_URL": "redis://127.0.0.1:6399/0",
}


class Counted:
    """A Redis client that counts its pings and hands every other call to the wrapped client."""

    def __init__(self, inner: Any, pings: list[int]) -> None:
        self.inner, self.pings = inner, pings

    def ping(self) -> Any:
        self.pings.append(1)
        return self.inner.ping()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class Slow:
    """A Redis client whose ping times out, as a Redis that answers slowly does."""

    def __init__(self, pings: list[int]) -> None:
        self.pings = pings

    def ping(self) -> None:
        import redis

        self.pings.append(1)
        raise redis.TimeoutError("Timeout reading from socket")


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(hook_redis, "client", hook_client)
    monkeypatch.setattr(hook_redis, "_clients", {})
    monkeypatch.setattr(modes, "CACHE_S", 5.0)
    return []


@pytest.fixture
def pings() -> list[int]:
    return []


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, built: list[dict[str, Any]], pings: list[int]) -> Any:
    import fakeredis
    import redis

    from scripts.swarm.store import RedisStore, SwarmConfig

    server = fakeredis.FakeServer()
    inner = fakeredis.FakeRedis(server=server, decode_responses=True)
    RedisStore(inner).create(SwarmConfig("demo", "/repo", max_eng=1, max_ci=0))
    RedisStore(inner).update("demo", gates={"watch": "off"})

    def from_url(url: str, **kwargs: Any) -> Counted:
        built.append({"url": url, **kwargs})
        return Counted(fakeredis.FakeRedis(server=server, decode_responses=True), pings)

    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    return inner


@pytest.mark.feature("hook-redis-reuse")
def test_one_hook_process_builds_one_redis_client_and_pings_it_once(
    fake: Any, built: list[dict[str, Any]], pings: list[int]
) -> None:
    from hooks.context import inbox_delivery, swarm_heartbeat

    fake.set("probe", "1")
    assert swarm_heartbeat.beat("working", dict(ENV))
    swarm_heartbeat.report("opus", "high", dict(ENV))
    swarm_heartbeat.heard("hello", dict(ENV))
    inbox_delivery.pending_context("s1", dict(ENV))
    inbox_delivery.confirm_shown("s1", dict(ENV))
    assert modes.swarm_gates("demo", dict(ENV)) == {"watch": "off"}
    assert len(built) == 1
    assert len(pings) == 1
    assert built[0]["url"] == ENV["AGENTIHOOKS_SWARM_REDIS_URL"]


@pytest.mark.feature("hook-redis-reuse")
def test_the_hook_client_uses_short_timeouts(fake: Any, built: list[dict[str, Any]]) -> None:
    hook_redis.client(dict(ENV))
    assert 0 < built[0]["socket_connect_timeout"] <= 1
    assert 0 < built[0]["socket_timeout"] <= 1
    assert built[0]["decode_responses"] is True


@pytest.mark.feature("hook-redis-reuse")
def test_a_refused_password_takes_the_store_hive_recovery_with_the_same_environment(
    monkeypatch: pytest.MonkeyPatch, built: list[dict[str, Any]]
) -> None:
    import redis

    class Refused:
        """A Redis client whose password the server refuses."""

        def ping(self) -> None:
            raise redis.AuthenticationError("WRONGPASS")

    recovered, seen = object(), []
    monkeypatch.setattr(redis.Redis, "from_url", lambda url, **kwargs: Refused())
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda environ: seen.append(environ) or recovered)
    env = dict(ENV)
    assert hook_redis.client(env) is recovered
    assert seen == [env] and seen[0] is env


@pytest.mark.feature("hook-redis-reuse")
def test_a_failed_hive_recovery_is_kept_for_the_process(
    monkeypatch: pytest.MonkeyPatch, built: list[dict[str, Any]]
) -> None:
    import redis

    class Refused:
        """A Redis client whose password the server refuses."""

        def ping(self) -> None:
            raise redis.AuthenticationError("WRONGPASS")

    def unrecovered(environ: Any) -> Any:
        built.append({})
        raise redis.ConnectionError("hive refresh failed")

    monkeypatch.setattr(redis.Redis, "from_url", lambda url, **kwargs: Refused())
    monkeypatch.setattr("scripts.swarm.store.redis_client", unrecovered)
    for _ in range(2):
        with pytest.raises(redis.ConnectionError):
            hook_redis.client(dict(ENV))
    assert len(built) == 1


@pytest.mark.feature("hook-redis-reuse")
def test_a_slow_redis_costs_the_short_timeout_once_per_process(
    monkeypatch: pytest.MonkeyPatch, built: list[dict[str, Any]], pings: list[int]
) -> None:
    import redis

    from hooks.context import inbox_delivery, swarm_heartbeat

    def from_url(url: str, **kwargs: Any) -> Slow:
        built.append(kwargs)
        return Slow(pings)

    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    for _ in range(2):
        with pytest.raises(redis.TimeoutError):
            swarm_heartbeat.beat("working", dict(ENV))
        assert inbox_delivery.pending_context("s1", dict(ENV)) == ""
    assert modes.swarm_gates("demo", dict(ENV)) is None
    assert (len(built), len(pings)) == (1, 1)


@pytest.mark.feature("hook-redis-reuse")
def test_gate_shims_inside_the_cache_window_do_not_reach_redis(
    fake: Any, built: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm.store import RedisStore

    assert modes.swarm_gates("demo", dict(ENV)) == {"watch": "off"}
    monkeypatch.setattr(hook_redis, "_clients", {})
    RedisStore(fake).update("demo", gates={"watch": "enforce"})
    assert modes.swarm_gates("demo", dict(ENV)) == {"watch": "off"}
    assert len(built) == 1
    monkeypatch.setattr(modes, "CACHE_S", 0)
    assert modes.swarm_gates("demo", dict(ENV)) == {"watch": "enforce"}
    assert len(built) == 2


def cache_file(home: Path) -> Path:
    """Return where the gate mode cache for the demo swarm on ENV's Redis lives under an agentihooks home."""
    source = hashlib.sha256(ENV["AGENTIHOOKS_SWARM_REDIS_URL"].encode()).hexdigest()[:12]
    return home / "swarm" / "demo" / f"gate-modes-{source}.json"


@pytest.mark.feature("hook-redis-reuse")
def test_the_gate_cache_lives_under_the_agentihooks_home_named_by_swarm_and_redis(
    fake: Any, built: list[dict[str, Any]], tmp_path: Path
) -> None:
    modes.swarm_gates("demo", dict(ENV))
    assert json.loads(cache_file(Path.home() / ".agentihooks").read_text())["gates"] == {"watch": "off"}
    modes.swarm_gates("demo", {**ENV, "AGENTIHOOKS_HOME": str(tmp_path / "home")})
    assert json.loads(cache_file(tmp_path / "home").read_text())["gates"] == {"watch": "off"}


@pytest.mark.feature("hook-redis-reuse")
def test_the_gate_cache_holds_from_its_write_until_the_window_ends_and_is_rewritten(
    fake: Any, built: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm.store import RedisStore

    clock = [100.0]
    monkeypatch.setattr(modes.time, "time", lambda: clock[0])
    assert modes.swarm_gates("demo", dict(ENV)) == {"watch": "off"}
    RedisStore(fake).update("demo", gates={"watch": "enforce"})
    assert modes.swarm_gates("demo", dict(ENV)) == {"watch": "off"}
    clock[0] = 100.0 + modes.CACHE_S
    assert modes.swarm_gates("demo", dict(ENV)) == {"watch": "enforce"}
    RedisStore(fake).update("demo", gates={"watch": "observe"})
    assert modes.swarm_gates("demo", dict(ENV)) == {"watch": "enforce"}
