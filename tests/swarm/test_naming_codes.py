import json

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.events.redis_state import RedisStateStore
from scripts.swarm import name_state, naming

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def bus(monkeypatch):
    opened = memory.open_bus(BusConfig())
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setattr(name_state, "_OPENED", {})
    monkeypatch.setattr(name_state.state, "open_state", opened.state)
    yield opened
    opened.close()


@pytest.fixture
def on_redis(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")


def _legacy(*parts):
    return naming.NameRegistry.key(*parts)


def _record(slug, code):
    return {"swarm": slug, "name": f"swarm@{code}", "ledger": slug, "repo": "/repo"}


def test_naming_errors_are_the_names_state_errors():
    assert naming.NamingError is name_state.NamingError


def test_swarm_codes_run_on_the_bus_with_no_redis_client(bus):
    names = naming.NameRegistry(None, mint=iter(["abcdef", "abcdef", "123456"]).__next__)
    assert names.mint_code("sw", "sw", "/repo") == "abcdef"
    assert names.mint_code("sw", "sw", "/repo") == "abcdef"
    assert names.code_of("sw") == "abcdef"
    assert names.swarm("abcdef") == _record("sw", "abcdef")
    assert names.swarm_slug("swarm@abcdef") == "sw"
    assert names.mint_code("other", "other", "/repo") == "123456"
    names.release("sw")
    assert names.code_of("sw") == ""
    assert names.swarm_slug("swarm@abcdef") == "swarm@abcdef"
    names.adopt("sw", "abcdef", "sw", "/repo")
    assert names.code_of("sw") == "abcdef"


def test_swarm_codes_written_on_redis_live_in_the_names_state_store(client, on_redis):
    names = naming.NameRegistry(client, mint=lambda: "abcdef")
    names.mint_code("sw", "sw", "/repo")
    shared = RedisStateStore(client, name_state.BUCKET)
    assert json.loads(shared.get(name_state.state_key("code-of", "sw")).value) == "abcdef"
    assert json.loads(shared.get(name_state.state_key("codes", "abcdef")).value) == _record("sw", "abcdef")
    assert not client.exists(_legacy("codes"))
    assert not client.exists(_legacy("code-of"))


def test_swarm_codes_written_before_the_move_still_read_and_release_hides_them(client, on_redis):
    client.hset(_legacy("codes"), "abcdef", json.dumps(_record("sw", "abcdef")))
    client.hset(_legacy("code-of"), "sw", "abcdef")
    names = naming.NameRegistry(client, mint=lambda: "123456")
    assert names.mint_code("sw", "sw", "/repo") == "abcdef"
    assert names.swarm("abcdef") == _record("sw", "abcdef")
    assert names.swarm_slug("swarm@abcdef") == "sw"
    names.release("sw")
    assert names.code_of("sw") == ""
    assert client.hget(_legacy("code-of"), "sw") == "abcdef"


def test_a_code_held_only_before_the_move_is_never_minted_again(client, on_redis):
    client.hset(_legacy("codes"), "abcdef", json.dumps({"swarm": "other"}))
    names = naming.NameRegistry(client, mint=iter(["abcdef", "123456"]).__next__)
    assert names.mint_code("sw", "sw", "/repo") == "123456"
    assert names.swarm("abcdef") == {"swarm": "other"}


def test_a_swarm_that_gained_a_code_meanwhile_keeps_it_and_frees_the_new_one(client, on_redis):
    names = naming.NameRegistry(client, mint=lambda: "123456")
    racing = naming.NameRegistry(client, mint=lambda: "abcdef")
    real = names.names_state.amend

    def amend(kind, part, change, legacy):
        if kind == "code-of":
            racing.mint_code("sw", "sw", "/repo")
        return real(kind, part, change, legacy)

    names.names_state.amend = amend
    assert names.mint_code("sw", "sw", "/repo") == "abcdef"
    assert names.swarm("123456") == {}
    assert names.code_of("sw") == "abcdef"


def test_no_free_code_after_every_try_is_a_naming_error(client, on_redis):
    names = naming.NameRegistry(client, mint=lambda: "abcdef")
    names.adopt("other", "abcdef", "other", "/repo")
    with pytest.raises(naming.NamingError, match=f"^no free swarm code after {naming.MINT_ATTEMPTS} tries$"):
        names.mint_code("sw", "sw", "/repo")


def test_adopt_keeps_a_code_already_registered(client, on_redis):
    names = naming.NameRegistry(client, mint=lambda: "abcdef")
    names.mint_code("sw", "sw", "/repo")
    names.adopt("sw", "abcdef", "elsewhere", "/other")
    names.adopt("sw", "123456", "sw", "/repo")
    assert names.swarm("abcdef") == _record("sw", "abcdef")
    assert names.code_of("sw") == "abcdef"


def test_swarm_slug_keeps_the_ref_when_the_store_fails(client, on_redis, monkeypatch, caplog):
    from redis.exceptions import RedisError

    names = naming.NameRegistry(client, mint=lambda: "abcdef")

    def down(*_):
        raise RedisError("down")

    monkeypatch.setattr(names.names_state.shared, "get", down)
    assert names.swarm_slug("swarm@abcdef") == "swarm@abcdef"
    assert [(r.name, r.levelname, r.getMessage()) for r in caplog.records] == [
        ("scripts.swarm.naming", "WARNING", "swarm alias lookup failed for swarm@abcdef: down")
    ]
