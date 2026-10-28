import json

import pytest

from hooks import config
from hooks.classifier import answer_cache

pytestmark = pytest.mark.unit

MODELS = ("liquid-d1", "jev-1.13")


def _key(**changes) -> str:
    fields = {"intent": "plain words", "question": "is it bad?", "finding": "task flt1", "models": MODELS}
    fields.update(changes)
    return answer_cache.key(**fields)


def test_the_cache_sits_next_to_the_decision_log():
    assert answer_cache.cache_path() == config.AGENTIHOOKS_HOME / "classifier" / "answers.json"


def test_the_key_is_a_hash_of_intent_question_finding_and_models():
    assert _key() == "0829a82775349e5a72e2507f536a0294dbb348492a10972df2e44c95a436c333"


@pytest.mark.parametrize(
    "change",
    [
        {"intent": "other words"},
        {"question": "is it fine?"},
        {"finding": "task flt2"},
        {"models": ("liquid-d1",)},
        {"models": ("jev-1.13", "liquid-d1")},
    ],
)
def test_each_part_of_the_key_changes_it(change):
    assert _key(**change) != _key()


def test_the_ttl_defaults_to_seven_days(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_FILTER_CACHE_TTL_S", raising=False)
    assert answer_cache.ttl_s() == 604800.0


def test_the_ttl_reads_its_setting(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_FILTER_CACHE_TTL_S", "90")
    assert answer_cache.ttl_s() == 90.0


def test_a_missing_cache_answers_nothing():
    assert answer_cache.lookup(["a"]) == {}


def test_stored_answers_come_back_with_their_time(monkeypatch):
    monkeypatch.setattr(answer_cache.time, "time", lambda: 500.0)
    answer_cache.store({"a": True, "b": False})
    assert answer_cache.lookup(["a", "b", "c"]) == {"a": True, "b": False}
    assert json.loads(answer_cache.cache_path().read_text()) == {
        "a": {"answer": True, "at": 500.0},
        "b": {"answer": False, "at": 500.0},
    }


def test_an_entry_expires_at_its_ttl(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_FILTER_CACHE_TTL_S", "10")
    monkeypatch.setattr(answer_cache.time, "time", lambda: 100.0)
    answer_cache.store({"a": True})
    monkeypatch.setattr(answer_cache.time, "time", lambda: 109.0)
    assert answer_cache.lookup(["a"]) == {"a": True}
    monkeypatch.setattr(answer_cache.time, "time", lambda: 110.0)
    assert answer_cache.lookup(["a"]) == {}


def test_storing_drops_expired_entries_and_keeps_live_ones(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_FILTER_CACHE_TTL_S", "10")
    monkeypatch.setattr(answer_cache.time, "time", lambda: 100.0)
    answer_cache.store({"old": True})
    monkeypatch.setattr(answer_cache.time, "time", lambda: 105.0)
    answer_cache.store({"mid": False})
    monkeypatch.setattr(answer_cache.time, "time", lambda: 112.0)
    answer_cache.store({"new": True})
    assert json.loads(answer_cache.cache_path().read_text()) == {
        "mid": {"answer": False, "at": 105.0},
        "new": {"answer": True, "at": 112.0},
    }


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"a": {"answer": "yes", "at": 1}}', '{"a": 3}'])
def test_an_unreadable_cache_answers_nothing(content, monkeypatch):
    monkeypatch.setattr(answer_cache.time, "time", lambda: 1.0)
    path = answer_cache.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert answer_cache.lookup(["a"]) == {}
    answer_cache.store({"b": True})
    assert json.loads(path.read_text()) == {"b": {"answer": True, "at": 1.0}}


def test_storing_nothing_writes_nothing():
    answer_cache.store({})
    assert not answer_cache.cache_path().exists()
