import os

import pytest
import redis

from hooks._redis import get_redis


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1"])
def test_no_test_reaches_a_redis_on_its_default_port(host):
    client = redis.Redis(host=host, port=6379, socket_connect_timeout=1)
    with pytest.raises(redis.ConnectionError, match="the test suite refuses"):
        client.ping()


def test_the_shell_redis_url_never_reaches_a_test():
    assert "REDIS_URL" not in os.environ
    assert get_redis() is None
