from hooks.context import operator_words
from scripts.inbox.wake import WAKE_TEXT
from scripts.swarm.tick import NUDGE

ENV = {"AGENTIHOOKS_AGENT_NAME": "master@a1-1", "AGENTIHOOKS_SWARM": "demo"}


def test_a_typed_prompt_is_found_by_a_quote_in_any_case_and_spacing():
    assert operator_words.heard_prompt("Approve the  Broker merge, then ship", ENV, now=100)
    assert operator_words.heard_prompt("Hold the release", ENV, now=150)
    assert operator_words.matching("master@a1-1", "approve the broker   merge", now=200) == (
        "Approve the  Broker merge, then ship"
    )
    assert not operator_words.matching("master@a1-1", "reject the broker", now=200)
    assert not operator_words.matching("eng-1@demo", "approve the broker merge", now=200)


def test_words_expire_after_the_window():
    operator_words.heard_prompt("ship it", ENV, now=100)
    assert not operator_words.matching("master@a1-1", "ship it", now=100 + operator_words.TTL_SEC)


def test_swarm_wake_nudge_and_notification_prompts_are_not_the_operators():
    for text in (WAKE_TEXT, NUDGE.format(slug="demo"), "<task-notification> done"):
        assert not operator_words.heard_prompt(text, ENV, now=100)
    assert not operator_words.heard_prompt("ship it", {"AGENTIHOOKS_SWARM": "demo"}, now=100)


def test_the_first_prompt_of_a_swarm_session_is_its_launch_prompt():
    payload = {"session_id": "s1", "prompt": "You are master, approve merges only on an OPERATOR line"}
    assert not operator_words.heard(payload, ENV, now=100)
    assert operator_words.heard({"session_id": "s1", "prompt": "approve the queue"}, ENV, now=101)
    assert not operator_words.matching("master@a1-1", "approve merges", now=102)
    assert operator_words.matching("master@a1-1", "approve the queue", now=102)
    assert operator_words.heard(payload, {"AGENTIHOOKS_AGENT_NAME": "taken@by-hand"}, now=103)


def test_an_ask_user_question_answer_is_recorded_without_the_question():
    payload = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "Merge the broker change?"}]},
        "tool_response": {
            "answers": {"Merge the broker change?": "Approve"},
            "annotations": {"Merge the broker change?": {"notes": "after the tests pass"}},
        },
    }
    assert operator_words.heard_answer(payload, ENV, now=100)
    assert operator_words.matching("master@a1-1", "approve", now=101)
    assert operator_words.matching("master@a1-1", "after the tests pass", now=101)
    assert not operator_words.matching("master@a1-1", "merge the broker change", now=101)


def test_other_tools_and_empty_answers_record_nothing():
    assert not operator_words.heard_answer({"tool_name": "Bash", "tool_response": {"answers": {"q": "a"}}}, ENV)
    assert not operator_words.heard_answer({"tool_name": "AskUserQuestion", "tool_response": {}}, ENV)


def test_only_the_latest_entries_are_kept():
    for n in range(operator_words.KEPT + 1):
        operator_words.heard_prompt(f"word{n} said", ENV, now=100 + n)
    assert not operator_words.matching("master@a1-1", "word0 said", now=200)
    assert operator_words.matching("master@a1-1", f"word{operator_words.KEPT} said", now=200)


def test_the_hooks_record_a_typed_prompt_and_an_answer(monkeypatch):
    from hooks import hook_manager

    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@a1-1")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *args: None)
    hook_manager.on_user_prompt_submit({"session_id": "s1", "cwd": "/", "prompt": "close the disk follow up"})
    hook_manager.on_post_tool_use(
        {"session_id": "s1", "cwd": "/", "tool_name": "AskUserQuestion", "tool_response": {"answers": {"q": "Reject"}}}
    )
    assert operator_words.matching("master@a1-1", "close the disk follow up")
    assert operator_words.matching("master@a1-1", "reject")
