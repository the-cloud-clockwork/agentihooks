import pytest

from scripts.swarm import naming

pytestmark = pytest.mark.unit


def test_the_registry_is_absent_without_a_redis_connection(monkeypatch):
    from hooks import _redis

    monkeypatch.setattr(_redis, "get_redis", lambda: None)
    assert naming.registry() is None


def test_the_registry_reads_the_shared_connection(monkeypatch):
    from hooks import _redis

    client = object()
    monkeypatch.setattr(_redis, "get_redis", lambda: client)
    found = naming.registry()
    assert type(found) is naming.NameRegistry
    assert found.redis is client
