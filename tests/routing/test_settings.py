import json
from pathlib import Path

import pytest

from scripts.routing import settings
from scripts.swarm import keyspace

NOW = 1_900_000_000.0


@pytest.fixture
def fake():
    import fakeredis

    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture
def home(tmp_path):
    (tmp_path / "ah").mkdir()
    return {"AGENTIHOOKS_HOME": str(tmp_path / "ah")}


@pytest.fixture(params=["redis", "file"])
def store(request, fake, home):
    return settings.open_store(fake if request.param == "redis" else None, home)


def test_defaults_are_zero_weights_and_unset_everything_else(store):
    assert store.all() == {"claude-api-weight": 0, "codex-api-weight": 0}
    assert store.get("codex-api-weight") == 0
    assert store.get("claude-api-max-sessions") is None
    assert store.history() == []


def test_a_write_is_read_back_and_recorded_with_its_actor_and_time(store):
    store.set("claude-api-weight", 25, "operator", NOW)
    store.set("master-account-codex", "work", "master", NOW + 1)
    store.set("codex-api-max-sessions", 0, "operator", NOW + 2)
    assert store.get("claude-api-weight") == 25
    assert store.all() == {
        "claude-api-weight": 25,
        "codex-api-weight": 0,
        "master-account-codex": "work",
        "codex-api-max-sessions": 0,
    }
    assert store.history() == [
        {"key": "claude-api-weight", "value": 25, "actor": "operator", "at": NOW},
        {"key": "master-account-codex", "value": "work", "actor": "master", "at": NOW + 1},
        {"key": "codex-api-max-sessions", "value": 0, "actor": "operator", "at": NOW + 2},
    ]


def test_none_unsets_a_key_and_is_recorded(store):
    store.set("claude-api-weight", 80, "operator", NOW)
    store.set("master-tier-claude", "max", "operator", NOW)
    store.set("claude-api-weight", None, "operator", NOW + 1)
    store.set("master-tier-claude", None, "operator", NOW + 1)
    assert store.get("claude-api-weight") == 0
    assert store.get("master-tier-claude") is None
    assert store.history()[-1] == {"key": "master-tier-claude", "value": None, "actor": "operator", "at": NOW + 1}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("claude-api-weight", 0),
        ("claude-api-weight", 100),
        ("codex-api-max-sessions", 0),
        ("claude-api-max-sessions", 12),
        ("master-account-claude", "a"),
        ("master-tier-codex", "pro"),
    ],
)
def test_valid_values_are_accepted(store, key, value):
    store.set(key, value, "operator", NOW)
    assert store.get(key) == value


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("claude-api-weight", -1),
        ("codex-api-weight", 101),
        ("claude-api-weight", 50.0),
        ("claude-api-weight", "50"),
        ("claude-api-weight", True),
        ("claude-api-max-sessions", -1),
        ("codex-api-max-sessions", 2.5),
        ("codex-api-max-sessions", False),
        ("master-account-claude", ""),
        ("master-account-codex", "  "),
        ("master-tier-claude", 3),
    ],
)
def test_invalid_writes_raise_and_leave_no_trace(store, key, value):
    with pytest.raises(ValueError) as raised:
        store.set(key, value, "operator", NOW)
    assert str(raised.value) == f"invalid value for {key}: {value!r}"
    assert store.all() == {"claude-api-weight": 0, "codex-api-weight": 0}
    assert store.history() == []


def test_a_write_needs_an_actor(store):
    with pytest.raises(ValueError) as raised:
        store.set("claude-api-weight", 5, "", NOW)
    assert str(raised.value) == "a routing setting write needs an actor"
    assert store.history() == []


def test_an_unknown_key_is_refused_on_read_and_write(store):
    with pytest.raises(ValueError) as raised:
        store.get("nope")
    assert str(raised.value) == "unknown routing setting nope"
    with pytest.raises(ValueError) as raised:
        store.set("unknown-key", 1, "operator", NOW)
    assert str(raised.value) == "unknown routing setting unknown-key"
    assert store.history() == []


def test_the_redis_store_keeps_the_hash_under_the_swarm_root(fake, home, tmp_path):
    settings.open_store(fake, home).set("claude-api-weight", 40, "operator", NOW)
    assert fake.hgetall(f"{keyspace.ROOT}:routing:settings") == {"claude-api-weight": "40"}
    assert fake.llen(f"{keyspace.ROOT}:routing:settings:history") == 1
    assert list((tmp_path / "ah").iterdir()) == []


def test_the_file_store_lives_in_the_agentihooks_home(home, tmp_path):
    settings.open_store(None, home).set("codex-api-weight", 10, "operator", NOW)
    data = json.loads((tmp_path / "ah" / "routing-settings.json").read_text())
    assert data == {
        "settings": {"codex-api-weight": 10},
        "history": [{"key": "codex-api-weight", "value": 10, "actor": "operator", "at": NOW}],
    }
    assert settings.open_store(None, home).get("codex-api-weight") == 10
    assert [p.name for p in (tmp_path / "ah").iterdir()] == ["routing-settings.json"]


def test_the_file_store_defaults_to_the_agentihooks_folder_in_the_user_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    settings.open_store(None, {}).set("codex-api-weight", 3, "operator", NOW)
    assert settings.open_store(None, {"AGENTIHOOKS_HOME": ""}).get("codex-api-weight") == 3
    assert (tmp_path / "user" / ".agentihooks" / "routing-settings.json").exists()
