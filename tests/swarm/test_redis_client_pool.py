from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.swarm import store


@pytest.fixture
def source():
    def connect(*args, **kwargs):
        return SimpleNamespace(ping=Mock())

    return SimpleNamespace(Redis=SimpleNamespace(from_url=Mock(side_effect=connect)))


def test_repeated_connections_reuse_the_pool_and_check_it_each_time(source):
    first = store._pinged(source, "rediss://localhost/0")
    second = store._pinged(source, "rediss://localhost/0")

    assert second is first
    assert first.ping.call_count == 2
    source.Redis.from_url.assert_called_once_with(
        "rediss://localhost/0", decode_responses=True, socket_connect_timeout=3, socket_timeout=10
    )


def test_changed_connection_settings_do_not_reuse_another_pool(source):
    first = store._pinged(source, "rediss://localhost/0")
    second = store._pinged(source, "rediss://localhost/1")

    assert second is not first
    assert store._pinged(source, "rediss://localhost/0") is first


def test_a_child_process_does_not_reuse_the_parent_pool(source, monkeypatch):
    monkeypatch.setattr(store.os, "getpid", lambda: 100)
    first = store._pinged(source, "rediss://localhost/0")
    monkeypatch.setattr(store.os, "getpid", lambda: 101)
    second = store._pinged(source, "rediss://localhost/0")

    assert second is not first
    assert store._pinged(source, "rediss://localhost/0") is second


def test_a_changed_factory_does_not_reuse_a_previous_client(source):
    first = store._pinged(source, "rediss://localhost/0")
    source.Redis.from_url = Mock(return_value=SimpleNamespace(ping=Mock()))
    second = store._pinged(source, "rediss://localhost/0")

    assert second is not first
    source.Redis.from_url.assert_called_once()


def test_a_reused_pool_does_not_hide_a_later_authentication_refusal(source):
    import redis

    first = store._pinged(source, "rediss://localhost/0")
    first.ping.side_effect = redis.AuthenticationError("refused")

    with pytest.raises(redis.AuthenticationError, match="^refused$"):
        store._pinged(source, "rediss://localhost/0")


def test_eight_current_endpoints_keep_their_pools(source):
    clients = [store._pinged(source, f"rediss://localhost/{database}") for database in range(8)]

    for database, client in enumerate(clients):
        assert store._pinged(source, f"rediss://localhost/{database}") is client


def test_old_endpoints_leave_the_bounded_pool_cache(source):
    first = store._pinged(source, "rediss://localhost/0")
    for database in range(1, 9):
        store._pinged(source, f"rediss://localhost/{database}")

    assert store._pinged(source, "rediss://localhost/0") is not first
