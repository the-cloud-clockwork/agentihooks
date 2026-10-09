import os

import pytest

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1"])
def test_no_test_reaches_a_redis_on_its_default_port(host):
    import redis

    client = redis.Redis(host=host, port=6379, socket_connect_timeout=1)
    with pytest.raises(redis.ConnectionError, match="the test suite refuses"):
        client.ping()


def test_the_shell_redis_url_never_reaches_a_test():
    from hooks._redis import get_redis

    assert "REDIS_URL" not in os.environ
    assert get_redis() is None


def test_fixture_lua_runs_only_on_an_isolated_fake_connection():
    import fakeredis

    from tests import redis_key_guard

    before = len(redis_key_guard.written)
    client = fakeredis.FakeRedis(decode_responses=True)
    assert client.eval("return redis.call('SET', KEYS[1], ARGV[1])", 1, "fixture-key", "fixture-value") == "OK"
    assert client.get("fixture-key") == "fixture-value"
    assert redis_key_guard.written[before:] == []


def test_real_redis_lua_is_refused_before_connecting(monkeypatch):
    import socket

    import redis

    from tests import redis_key_guard

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        client = redis.Redis(host="127.0.0.1", port=reserved.getsockname()[1])
        dispatched = []
        monkeypatch.setattr(client, "_execute_command", lambda *args, **kwargs: dispatched.append(args))
        before = len(redis_key_guard.written)
        with pytest.raises(redis_key_guard.ProductionKey) as error:
            client.eval("return redis.call('SET', KEYS[1], ARGV[1])", 1, "fixture-key", "fixture-value")
        caught = redis_key_guard.written[before:]
        del redis_key_guard.written[before:]
    assert caught == ["EVAL"]
    assert str(error.value) == "a test sent EVAL, which can reach production keys the guard cannot read"
    assert dispatched == []
