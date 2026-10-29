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


class _Aliases:
    def __init__(self, failure=None):
        self.failure, self.reads = failure, []

    def get(self, key):
        self.reads.append(key)
        if self.failure is not None:
            raise self.failure
        return None


def test_a_registered_slug_is_absent_without_a_registry(monkeypatch):
    from hooks import _redis

    monkeypatch.setattr(_redis, "get_redis", lambda: None)
    assert naming.registered_slug("master@abcdef-0001") is None


def test_a_registered_slug_reads_the_alias_then_the_swarm(monkeypatch):
    from hooks import _redis

    client = _Aliases()
    monkeypatch.setattr(_redis, "get_redis", lambda: client)
    monkeypatch.setattr(naming.NameRegistry, "slug_of", lambda self, name: f"{name} in demo")
    assert naming.registered_slug("master@abcdef-0001") == "master@abcdef-0001 in demo"
    assert client.reads == [naming.NameRegistry.key("alias", "master@abcdef-0001")]


def test_a_registered_slug_raises_a_failed_alias_read(monkeypatch):
    from hooks import _redis

    failure = OSError("alias read timed out")
    monkeypatch.setattr(_redis, "get_redis", lambda: _Aliases(failure))
    monkeypatch.setattr(naming.NameRegistry, "slug_of", lambda self, name: pytest.fail("slug_of swallows the failure"))
    with pytest.raises(OSError) as raised:
        naming.registered_slug("master@abcdef-0001")
    assert raised.value is failure
