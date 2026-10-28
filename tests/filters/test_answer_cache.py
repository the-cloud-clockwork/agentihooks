import pytest

from hooks.classifier import ClassifierError, answer_cache
from hooks.context import conditions

pytestmark = pytest.mark.unit

FILTER = """intent: {intent}
action: flag
finders:
  - regex: 'task [a-z]+[0-9]+'
    reason: names a task id
"""
INTENT = "user facing text names no internal ids"


def _write_call(content: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "sid-filters",
        "tool_name": "Write",
        "tool_input": {"file_path": "/repo/page.py", "content": content},
        "permission_mode": "default",
        "cwd": "/tmp",
    }


def _install(filters_dir, intent: str = INTENT) -> None:
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER.format(intent=intent))


def _findings(call: dict) -> list[str]:
    return [question.instructions.split("\n")[2] for question in call["questions"].values()]


def test_a_second_identical_run_asks_the_classifier_nothing(filters_dir, stub):
    _install(filters_dir)
    fake = stub(yes=True)
    first = conditions.pre_effect(_write_call("waiting on task flt1"))
    second = conditions.pre_effect(_write_call("waiting on task flt1"))
    assert len(fake.calls) == 1
    assert second.contexts == first.contexts
    assert second.contexts == ['[condition pre-write-ids.filter.yaml] filter flagged:\n- "task flt1": names a task id']


def test_a_cached_no_answer_lets_the_repeat_through_without_asking(filters_dir, stub):
    _install(filters_dir)
    fake = stub(yes=False)
    conditions.pre_effect(_write_call("waiting on task flt1"))
    second = conditions.pre_effect(_write_call("waiting on task flt1"))
    assert len(fake.calls) == 1
    assert second.contexts == []
    assert second.block is None


def test_a_repeat_asks_only_the_uncached_findings(filters_dir, stub):
    _install(filters_dir)
    fake = stub(yes=True)
    conditions.pre_effect(_write_call("waiting on task flt1"))
    second = conditions.pre_effect(_write_call("task flt1 then task flt2"))
    assert len(fake.calls) == 2
    assert list(fake.calls[1]["questions"]) == ["finding_0"]
    assert _findings(fake.calls[1]) == ["Finding: task flt2"]
    assert second.contexts == [
        "[condition pre-write-ids.filter.yaml] filter flagged:\n"
        '- "task flt1": names a task id\n- "task flt2": names a task id'
    ]


def test_a_change_of_intent_text_misses_the_cache(filters_dir, stub):
    _install(filters_dir)
    fake = stub(yes=True)
    conditions.pre_effect(_write_call("waiting on task flt1"))
    _install(filters_dir, intent="user facing text names no ids at all")
    conditions.pre_effect(_write_call("waiting on task flt1"))
    assert len(fake.calls) == 2
    assert fake.calls[1]["state"]["intent"] == "user facing text names no ids at all"


def test_a_change_of_model_set_misses_the_cache(filters_dir, stub, monkeypatch):
    _install(filters_dir)
    fake = stub(yes=True)
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", "liquid-d1")
    conditions.pre_effect(_write_call("waiting on task flt1"))
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", "liquid-d1,jev-1.13")
    conditions.pre_effect(_write_call("waiting on task flt1"))
    assert len(fake.calls) == 2


def test_an_expired_entry_is_asked_again(filters_dir, stub, monkeypatch):
    _install(filters_dir)
    fake = stub(yes=True)
    monkeypatch.setenv("AGENTIHOOKS_FILTER_CACHE_TTL_S", "60")
    monkeypatch.setattr(answer_cache.time, "time", lambda: 1000.0)
    conditions.pre_effect(_write_call("waiting on task flt1"))
    monkeypatch.setattr(answer_cache.time, "time", lambda: 1059.0)
    conditions.pre_effect(_write_call("waiting on task flt1"))
    assert len(fake.calls) == 1
    monkeypatch.setattr(answer_cache.time, "time", lambda: 1060.0)
    conditions.pre_effect(_write_call("waiting on task flt1"))
    assert len(fake.calls) == 2


def test_an_unavailable_classifier_caches_nothing(filters_dir, stub):
    _install(filters_dir)
    stub(error=ClassifierError("no decision backend answered"))
    conditions.pre_effect(_write_call("waiting on task flt1"))
    fake = stub(yes=True)
    effect = conditions.pre_effect(_write_call("waiting on task flt1"))
    assert len(fake.calls) == 1
    assert effect.contexts == ['[condition pre-write-ids.filter.yaml] filter flagged:\n- "task flt1": names a task id']


def test_cached_yes_answers_still_flag_while_the_classifier_is_down(filters_dir, stub):
    _install(filters_dir)
    stub(yes=True)
    conditions.pre_effect(_write_call("waiting on task flt1"))
    fake = stub(error=ClassifierError("no decision backend answered"))
    effect = conditions.pre_effect(_write_call("task flt1 then task flt2"))
    assert len(fake.calls) == 1
    assert effect.contexts == ['[condition pre-write-ids.filter.yaml] filter flagged:\n- "task flt1": names a task id']
