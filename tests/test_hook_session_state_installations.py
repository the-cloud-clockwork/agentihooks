import json

import fakeredis
import pytest

from hooks._redis import _KEY_PREFIX
from hooks.context import branch_guard, file_read_cache, retry_breaker
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
        monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
        monkeypatch.setattr(retry_breaker, "_memory_state", {})

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
    use(first)
    record = keyspace.installation(first)
    assert event_relay._position_key(SESSION) == f"{_KEY_PREFIX}:{record.installation_id}:pos:eventrelay:{SESSION}"


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"created_at": "x"}), json.dumps(["inst"])])
def test_the_event_relay_cursor_key_without_an_installation_record_stays_unscoped(installations, content):
    (first, _), use = installations
    use(first)
    if content is not None:
        first.mkdir(parents=True)
        (first / keyspace.INSTALLATION_FILE).write_text(content)
    assert event_relay._position_key(SESSION) == f"{_KEY_PREFIX}:pos:eventrelay:{SESSION}"
