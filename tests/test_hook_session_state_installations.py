import json

import fakeredis
import pytest

from hooks._redis import _KEY_PREFIX, redis_key
from hooks.context import branch_guard, file_read_cache, retry_breaker
from hooks.memory.store import MemoryStore
from hooks.observability import event_relay
from scripts.swarm_v2 import keyspace

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SESSION = "4242"


@pytest.fixture
def redis(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True)
    for module in (file_read_cache, retry_breaker, branch_guard):
        monkeypatch.setattr(module, "get_redis", lambda: fake)
    monkeypatch.setattr(event_relay, "_get_redis", lambda: fake)
    return fake


@pytest.fixture
def installations(tmp_path, monkeypatch):
    homes = (tmp_path / "first", tmp_path / "second")

    def use(home):
        monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", home)
        monkeypatch.setattr(branch_guard, "AGENTIHOOKS_HOME", home)
        monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
        monkeypatch.setattr(retry_breaker, "_memory_state", {})
        return keyspace.installation(home)

    return homes, use


def test_two_installations_with_one_session_number_keep_separate_hook_state(redis, installations, tmp_path):
    (first, second), use = installations
    read = tmp_path / "read.txt"
    read.write_text("x")
    failing = {"count": 3, "last_error_key": "e", "last_error_text": "boom", "last_input": "ls"}

    use(first)
    file_read_cache.mark_file_read(SESSION, str(read))
    retry_breaker._set_state(SESSION, "op", failing)
    branch_guard.set_branch_signal(SESSION)
    event_relay._save_position(SESSION, 100)

    use(second)
    assert file_read_cache.was_file_read(SESSION, str(read)) is False
    assert retry_breaker._get_state(SESSION, "op") == retry_breaker._default_state()
    assert branch_guard._has_branch_signal(SESSION) is False
    assert event_relay._load_position(SESSION) == 0
    event_relay._save_position(SESSION, 7)

    use(first)
    assert file_read_cache.was_file_read(SESSION, str(read)) is True
    assert retry_breaker._get_state(SESSION, "op") == failing
    assert branch_guard._has_branch_signal(SESSION) is True
    assert event_relay._load_position(SESSION) == 100


def test_the_event_relay_cursor_key_carries_the_installation(installations):
    (first, _), use = installations
    record = use(first)
    assert event_relay._position_key(SESSION) == f"{_KEY_PREFIX}:{record.installation_id}:pos:eventrelay:{SESSION}"


@pytest.mark.parametrize(
    "content",
    [
        None,
        "not json",
        json.dumps({"created_at": "x"}),
        json.dumps(["inst"]),
        json.dumps({"installation_id": ""}),
        json.dumps({"installation_id": "x"}),
        json.dumps({"installation_id": 7}),
        json.dumps({"installation_id": f"inst-{'a' * 32}x"}),
    ],
)
def test_the_event_relay_cursor_key_without_a_valid_installation_record_stays_unscoped(tmp_path, monkeypatch, content):
    home = tmp_path / "home"
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    if content is not None:
        home.mkdir()
        (home / keyspace.INSTALLATION_FILE).write_text(content)
    assert event_relay._position_key(SESSION) == f"{_KEY_PREFIX}:pos:eventrelay:{SESSION}"


def test_the_event_relay_reads_the_installation_from_the_default_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    record = keyspace.installation(tmp_path / ".agentihooks")
    assert event_relay._position_key(SESSION) == f"{_KEY_PREFIX}:{record.installation_id}:pos:eventrelay:{SESSION}"
    assert event_relay._position_file(SESSION) == tmp_path / ".agentihooks" / "event_relay_positions" / f"{SESSION}.pos"


def test_a_cursor_saved_before_the_installation_scope_still_resumes(redis, installations):
    (first, _), use = installations
    use(first)
    redis.set(f"{_KEY_PREFIX}:pos:eventrelay:{SESSION}", "55")
    assert event_relay._load_position(SESSION) == 55
    event_relay._save_position(SESSION, 60)
    assert event_relay._load_position(SESSION) == 60
    assert redis.get(f"{_KEY_PREFIX}:pos:eventrelay:{SESSION}") == "55"


def test_two_installations_with_one_session_number_keep_separate_memory_session_indexes(redis, installations):
    (first, second), use = installations
    store = MemoryStore()
    store._redis, store._redis_checked = redis, True

    use(first)
    kept = store.save("first", session_id=SESSION)
    dropped = store.save("first again", session_id=SESSION)
    assert redis.smembers(redis_key("memory:idx:session", SESSION)) == {kept.id, dropped.id}

    use(second)
    assert store.recall(session_id=SESSION) == []
    other = store.save("second", session_id=SESSION)
    assert [m.id for m in store.recall(session_id=SESSION)] == [other.id]

    use(first)
    assert store.delete(dropped.id) is True
    assert [m.id for m in store.recall(session_id=SESSION)] == [kept.id]
    store.clear()
    assert redis.smembers(redis_key("memory:idx:session", SESSION)) == set()
