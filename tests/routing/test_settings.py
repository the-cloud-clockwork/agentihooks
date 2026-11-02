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


@pytest.fixture
def state():
    from scripts.events import memory
    from scripts.events.config import BusConfig

    bus = memory.open_bus(BusConfig())
    yield bus.state(settings.BUCKET)
    bus.close()


@pytest.fixture(params=["state", "file"])
def store(request, state, home):
    return settings.open_store(state if request.param == "state" else None, home)


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


def test_a_claim_sets_an_unset_key_once_and_returns_the_holder(store):
    assert store.claim("master-account-claude", "a", "master@x", NOW) == "a"
    assert store.claim("master-account-claude", "b", "master@y", NOW + 1) == "a"
    assert store.get("master-account-claude") == "a"
    assert store.history() == [{"key": "master-account-claude", "value": "a", "actor": "master@x", "at": NOW}]


@pytest.mark.parametrize(
    ("value", "actor", "said"),
    [
        (None, "m", "^invalid value for master-account-claude: None$"),
        ("", "m", "^invalid value for master-account-claude: ''$"),
        ("a", "", "^a routing setting write needs an actor$"),
    ],
)
def test_a_claim_refuses_an_empty_value_or_actor(store, value, actor, said):
    with pytest.raises(ValueError, match=said):
        store.claim("master-account-claude", value, actor, NOW)
    with pytest.raises(ValueError, match="unknown routing setting"):
        store.claim("nope", "a", "m", NOW)
    assert store.history() == []


def test_a_file_claim_creates_missing_parent_folders(tmp_path):
    store = settings.FileSettings(tmp_path / "a" / "b" / "routing-settings.json")
    assert store.claim("master-account-claude", "a", "m", NOW) == "a"
    assert store.get("master-account-claude") == "a"


def test_the_file_claim_holds_a_lock_beside_the_settings(home, monkeypatch):
    locked = []
    monkeypatch.setattr(settings.fcntl, "flock", lambda handle, mode: locked.append((Path(handle.name).name, mode)))
    settings.open_store(None, home).claim("master-account-claude", "a", "m", NOW)
    assert locked == [("routing-settings.json.lock", settings.fcntl.LOCK_EX)]


def test_an_unknown_key_is_refused_on_read_and_write(store):
    with pytest.raises(ValueError) as raised:
        store.get("nope")
    assert str(raised.value) == "unknown routing setting nope"
    with pytest.raises(ValueError) as raised:
        store.set("unknown-key", 1, "operator", NOW)
    assert str(raised.value) == "unknown routing setting unknown-key"
    assert store.history() == []


def test_the_state_store_keeps_settings_and_history_in_one_routing_entry(state, home, tmp_path):
    settings.open_store(state, home).set("claude-api-weight", 40, "operator", NOW)
    assert json.loads(state.get(settings.ENTRY).value) == {
        "settings": {"claude-api-weight": 40},
        "history": [{"key": "claude-api-weight", "value": 40, "actor": "operator", "at": NOW}],
    }
    assert settings.ENTRY == "settings"
    assert settings.BUCKET == "routing"
    assert list((tmp_path / "ah").iterdir()) == []


def _legacy(fake):
    from scripts.events.redis_state import RedisStateStore

    root = f"{keyspace.ROOT}:routing:settings"
    fake.hset(root, "codex-api-weight", "30")
    fake.rpush(f"{root}:history", json.dumps({"key": "codex-api-weight", "value": 30, "actor": "operator", "at": 1.0}))
    return RedisStateStore(fake, settings.BUCKET)


def test_the_redis_state_store_seeds_from_the_legacy_hash_until_its_first_write(fake, home):
    store = settings.open_store(_legacy(fake), home)
    assert store.all() == {"claude-api-weight": 0, "codex-api-weight": 30}
    assert store.history() == [{"key": "codex-api-weight", "value": 30, "actor": "operator", "at": 1.0}]
    store.set("claude-api-weight", 5, "operator", NOW)
    fake.hset(f"{keyspace.ROOT}:routing:settings", "codex-api-weight", "99")
    assert store.all() == {"claude-api-weight": 5, "codex-api-weight": 30}
    assert [entry["key"] for entry in store.history()] == ["codex-api-weight", "claude-api-weight"]


def test_a_claim_sees_a_legacy_holder(fake, home):
    fake.hset(f"{keyspace.ROOT}:routing:settings", "master-account-claude", json.dumps("old"))
    store = settings.open_store(_legacy(fake), home)
    assert store.claim("master-account-claude", "new", "master@x", NOW) == "old"
    assert store.state.get(settings.ENTRY) is None


def test_the_legacy_seed_reads_the_whole_history_list(fake, home):
    from scripts.events.redis_state import RedisStateStore

    rows = [{"key": "codex-api-weight", "value": value, "actor": "operator", "at": float(value)} for value in (1, 2, 3)]
    fake.rpush(f"{keyspace.ROOT}:routing:settings:history", *[json.dumps(row) for row in rows])
    assert settings.open_store(RedisStateStore(fake, settings.BUCKET), home).history() == rows


def test_a_state_store_without_legacy_rows_reads_empty(state, home):
    assert settings.StateSettings(state)._load() == ({"settings": {}, "history": []}, None)


class _Racing:
    def __init__(self, state, races):
        self.state, self.races, self.writes = state, races, 0

    def get(self, key):
        return self.state.get(key)

    def _race(self):
        self.writes += 1
        if self.races:
            self.races -= 1
            settings.StateSettings(self.state).set("codex-api-weight", self.writes, "rival", NOW)

    def create(self, key, value):
        self._race()
        return self.state.create(key, value)

    def update(self, key, value, revision):
        self._race()
        return self.state.update(key, value, revision)


def test_a_write_that_lost_a_race_is_retried_on_the_fresh_entry(state, home):
    racing = _Racing(state, 2)
    settings.open_store(racing, home).set("claude-api-weight", 7, "operator", NOW)
    assert settings.StateSettings(state).all() == {"claude-api-weight": 7, "codex-api-weight": 2}
    assert [entry["actor"] for entry in settings.StateSettings(state).history()] == ["rival", "rival", "operator"]
    assert racing.writes == 3


def test_a_claim_that_lost_a_race_returns_the_rival_holder(state, home):
    class Rival(_Racing):
        def _race(self):
            if self.races:
                self.races -= 1
                settings.StateSettings(self.state).claim("master-account-claude", "rival", "m", NOW)

    assert settings.open_store(Rival(state, 1), home).claim("master-account-claude", "me", "m", NOW) == "rival"
    assert len(settings.StateSettings(state).history()) == 1


def test_a_write_that_keeps_losing_raises_the_last_conflict(state, home):
    from scripts.events.bus import StateConflict

    racing = _Racing(state, settings.WRITE_TRIES)
    with pytest.raises(StateConflict) as raised:
        settings.open_store(racing, home).set("claude-api-weight", 7, "operator", NOW)
    assert (
        str(raised.value)
        == f"state key 'settings' is at revision {settings.WRITE_TRIES}, not {settings.WRITE_TRIES - 1}"
    )
    assert racing.writes == settings.WRITE_TRIES
    assert settings.WRITE_TRIES == 8


@pytest.mark.parametrize("module", ["scripts/routing/settings.py", "scripts/routing/place.py"])
def test_no_routing_placement_module_imports_the_redis_client(module):
    import ast

    tree = ast.parse((Path(__file__).parents[2] / module).read_text())
    imported = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    imported += [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module]
    assert [name for name in imported if name.split(".")[0] == "redis"] == []


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
