import time

import hooks.config
from hooks import hook_manager
from hooks.context import operator_words
from hooks.targets.normalizer import normalize_payload
from scripts.inbox.wake import WAKE_TEXT
from scripts.swarm.tick import NUDGE

ENV = {"AGENTIHOOKS_AGENT_NAME": "master@a1-1", "AGENTIHOOKS_SWARM": "demo"}
LATER = 100 + operator_words.TTL_SEC


def test_a_typed_prompt_is_found_by_a_quote_in_any_case_and_spacing():
    assert operator_words.heard_prompt("Approve the  Broker merge, then ship", ENV, now=100)
    assert operator_words.heard_prompt("Hold the release", ENV, now=150)
    assert operator_words.matching("master@a1-1", "approve the broker   merge", now=200) == (
        "Approve the  Broker merge, then ship"
    )
    assert operator_words.matching("master@a1-1", "reject the broker", now=200) == ""
    assert operator_words.matching("eng-1@demo", "approve the broker merge", now=200) == ""


def test_a_quote_matches_the_words_as_typed_and_not_their_upper_case_spelling():
    assert operator_words.record("master@a1-1", "Use the Straße route", now=100)
    assert operator_words.matching("master@a1-1", "strasse", now=101) == ""
    assert operator_words.matching("master@a1-1", "STRAßE route", now=101) == "Use the Straße route"


def test_words_are_kept_per_agent_under_the_state_home(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path / "fresh" / "home")
    assert operator_words.record("Master A/b c", "ship it", now=100)
    assert [p.name for p in (tmp_path / "fresh" / "home" / "operator_words").iterdir()] == ["words.sqlite3"]
    assert operator_words.matching("Master A/b c", "ship it", now=101) == "ship it"


def test_an_unreadable_record_holds_no_words(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
    (tmp_path / "operator_words").mkdir()
    (tmp_path / "operator_words" / "master@a1-1.json").write_text("{not json")
    assert operator_words.matching("master@a1-1", "ship", now=100) == ""
    assert operator_words.record("master@a1-1", "ship it", now=100)


def test_words_expire_after_the_window():
    operator_words.heard_prompt("ship it", ENV, now=100)
    assert operator_words.matching("master@a1-1", "ship it", now=LATER) == ""


def test_swarm_wake_nudge_and_notification_prompts_are_not_the_operators():
    for text in (WAKE_TEXT, NUDGE.format(slug="demo"), "<task-notification> done"):
        assert not operator_words.heard_prompt(text, ENV, now=100)
    assert not operator_words.heard_prompt("ship it", {"AGENTIHOOKS_SWARM": "demo"}, now=100)


def test_the_first_prompt_of_a_swarm_session_is_its_launch_prompt():
    payload = {"session_id": "s1", "prompt": "You are master, approve merges only on an OPERATOR line"}
    assert not operator_words.heard(payload, ENV, now=100)
    assert operator_words.heard({"session_id": "s1", "prompt": "approve the queue"}, ENV, now=101)
    assert operator_words.matching("master@a1-1", "approve merges", now=102) == ""
    assert operator_words.matching("master@a1-1", "approve the queue", now=102) == "approve the queue"
    assert operator_words.heard(payload, {"AGENTIHOOKS_AGENT_NAME": "taken@by-hand"}, now=103)


def test_typed_holds_only_the_operators_own_words_and_records_them_in_a_named_session():
    assert not operator_words.typed({"session_id": "s1", "prompt": "You are master, set the git guard condition"}, ENV)
    assert operator_words.typed({"session_id": "s1", "prompt": "set the git guard condition"}, ENV, now=101)
    assert not operator_words.typed({"session_id": "s1", "prompt": "<task-notification> set it"}, ENV, now=102)
    assert operator_words.matching("master@a1-1", "git guard", now=103) == "set the git guard condition"
    assert operator_words.matching("master@a1-1", "git guard", now=101 + operator_words.TTL_SEC) == ""


def test_typed_in_an_unnamed_session_is_any_prompt_the_swarm_did_not_send():
    swarm = {"AGENTIHOOKS_SWARM": "demo"}
    assert operator_words.typed({"prompt": "set the git guard condition"}, swarm)
    assert operator_words.typed({"prompt": "set the git guard condition"}, {})
    for text in (WAKE_TEXT, NUDGE.format(slug="demo"), "<task-notification> done", ""):
        assert not operator_words.typed({"prompt": text}, swarm)
    assert not operator_words.typed({"tool_name": "AskUserQuestion", "tool_response": {"answers": {"q": "a"}}}, {})


def test_a_prompt_without_a_session_id_is_recorded():
    assert operator_words.heard({"prompt": "hold the merge"}, ENV, now=100)
    assert operator_words.matching("master@a1-1", "hold the merge", now=LATER - 1) == "hold the merge"
    assert operator_words.matching("master@a1-1", "hold the merge", now=LATER) == ""


def test_an_ask_user_question_answer_is_recorded_without_the_question(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    payload = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "Merge the broker change?"}]},
        "tool_response": {
            "answers": {"Merge the broker change?": "Approve"},
            "annotations": {"Merge the broker change?": {"notes": "after the tests pass"}},
        },
    }
    assert operator_words.heard(payload, ENV, now=100)
    assert operator_words.matching("master@a1-1", "approve", now=101) == "Approve\nafter the tests pass"
    assert operator_words.matching("master@a1-1", "merge the broker change", now=101) == ""
    assert operator_words.matching("master@a1-1", "approve", now=LATER) == ""


def test_answers_carried_only_in_the_tool_input_are_recorded():
    payload = {"tool_name": "AskUserQuestion", "tool_input": {"answers": {"q": "Hold"}}, "tool_response": "ok"}
    assert operator_words.heard_answer(payload, ENV, now=100)
    assert operator_words.matching("master@a1-1", "hold", now=101) == "Hold"


def test_other_tools_empty_answers_and_unnamed_sessions_record_nothing():
    assert not operator_words.heard_answer({"tool_name": "Bash", "tool_response": {"answers": {"q": "a"}}}, ENV)
    assert not operator_words.heard_answer({"tool_name": "AskUserQuestion", "tool_response": {}}, ENV)
    answer = {"tool_name": "AskUserQuestion", "tool_response": {"answers": {"q": "Approve"}}}
    assert not operator_words.heard_answer(answer, {}, now=100)


def test_the_five_hundred_and_first_oldest_prompt_still_relays():
    from scripts.swarm_ledger import ledger_relay

    for n in range(501):
        operator_words.heard_prompt(f"word{n} said", ENV, now=100 + n)
    assert ledger_relay.verified("master@a1-1", "word0 said") == "word0 said"
    assert ledger_relay.verified("master@a1-1", "word500 said") == "word500 said"


def test_line_hash_lookup_obeys_names_and_the_window():
    operator_words.record("master@a1-1", "Preface\nShip  it\nEnd", now=100)
    operator_words.record("master@a1-1", "Ship it\nLatest", now=101)
    with operator_words._store() as connection:
        assert operator_words._line_match(connection, "master@a1-1", "ship it", None) == ("Ship it\nLatest",)
        assert operator_words._line_match(connection, "master@a1-1", "ship it", 101) is None
        assert operator_words._line_match(connection, "master@a1-1", "ship it", 100) == ("Ship it\nLatest",)
        assert operator_words._line_match(connection, "master@a1-1", "ship", None) is None
        assert operator_words._line_match(connection, "master@b2-1", "ship it", None) is None


def test_each_line_is_indexed_with_its_sha256_hash():
    assert operator_words._hash("ship it") == "bef4261f394bf71fd2b565cd76396ac9ed7953f9110c69ee49d7a82871238fbf"


def test_retained_json_words_relay_after_upgrade_and_are_removed_on_forget(tmp_path, monkeypatch):
    import json

    from scripts.swarm_ledger import ledger_relay

    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
    folder = tmp_path / "operator_words"
    folder.mkdir()
    path = folder / "demo-master-1.json"
    path.write_text(json.dumps({"sessions": ["old-session"], "rows": [{"at": 100, "words": "Keep the old quote"}]}))
    assert operator_words.recorded("demo-master-*") == ["demo-master-1"]
    assert ledger_relay.verified("demo-master-1", "Keep the old quote") == "Keep the old quote"
    assert json.loads(path.read_text()) == {"sessions": ["old-session"]}
    assert not operator_words.heard_prompt(
        "new words",
        {"AGENTIHOOKS_AGENT_NAME": "demo-master-1", "AGENTIHOOKS_SWARM": "demo"},
        now=101,
        session="new-session",
    )
    assert ledger_relay.verified("demo-master-1", "Keep the old quote") == "Keep the old quote"
    operator_words.forget("demo")
    assert ledger_relay.verified("demo-master-1", "Keep the old quote") == ""
    assert operator_words.recorded("demo-master-*") == []


def test_upgrade_uses_the_registered_swarm_for_modern_agent_names(tmp_path, monkeypatch):
    import json

    import fakeredis

    from hooks import _redis
    from scripts.swarm.naming import NameRegistry

    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
    redis = fakeredis.FakeRedis(decode_responses=True)
    NameRegistry(redis).adopt("demo", "abcdef", "ledger", "repo")
    monkeypatch.setattr(_redis, "get_redis", lambda: redis)
    folder = tmp_path / "operator_words"
    folder.mkdir()
    path = folder / "master@abcdef-0001.json"
    path.write_text(json.dumps({"sessions": [], "rows": [{"at": 100, "words": "Registered old words"}]}))
    assert operator_words.matching("master@abcdef-0001", "Registered old words", now=101) == "Registered old words"
    assert json.loads(path.read_text()) == {"sessions": []}
    operator_words.forget("demo")
    assert operator_words.matching("master@abcdef-0001", "Registered old words", within=None) == ""


def test_upgrade_retry_keeps_one_copy_when_cleaning_the_old_file_failed(tmp_path, monkeypatch):
    import json

    import pytest

    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
    folder = tmp_path / "operator_words"
    folder.mkdir()
    path = folder / "demo-master-1.json"
    path.write_text(json.dumps({"sessions": [], "rows": [{"at": 100, "words": "Once"}]}))
    save = operator_words._save

    def fail_save(name, data):
        raise OSError("interrupted cleanup")

    monkeypatch.setattr(operator_words, "_save", fail_save)
    with pytest.raises(OSError, match="interrupted cleanup"):
        operator_words.matching("demo-master-1", "Once", now=101)
    monkeypatch.setattr(operator_words, "_save", save)
    assert operator_words.matching("demo-master-1", "Once", now=101) == "Once"
    with operator_words._store() as connection:
        assert connection.execute("SELECT swarm, name, at, words, norm FROM entries").fetchall() == [
            ("demo", "demo-master-1", 100.0, "Once", "once")
        ]
    assert json.loads(path.read_text()) == {"sessions": []}


def test_whole_line_lookup_precedes_a_newer_substring_match():
    operator_words.record("master@a1-1", "Ship it", now=100)
    operator_words.record("master@a1-1", "Do not ship it", now=101)
    assert operator_words.matching("master@a1-1", "ship it", now=102) == "Ship it"
    assert operator_words.matching("master@a1-1", "ship", now=102) == "Do not ship it"


def test_words_outside_the_window_are_kept_and_found_with_no_window():
    operator_words.heard_prompt("ship it", ENV, now=100)
    operator_words.record("master@a1-1", "hold it", now=LATER)
    assert operator_words.matching("master@a1-1", "ship it", now=LATER) == ""
    assert operator_words.matching("master@a1-1", "ship it", now=LATER * 1000, within=None) == "ship it"
    assert operator_words.recorded("master@*") == ["master@a1-1"]


def test_the_hooks_record_a_typed_prompt_and_an_answer(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@a1-1")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *args: None)
    hook_manager.on_user_prompt_submit({"session_id": "s1", "cwd": "/", "prompt": "close the disk follow up"})
    hook_manager.on_post_tool_use(
        {"session_id": "s1", "cwd": "/", "tool_name": "AskUserQuestion", "tool_response": {"answers": {"q": "Reject"}}}
    )
    assert operator_words.matching("master@a1-1", "close the disk follow up")
    assert operator_words.matching("master@a1-1", "reject")


CODEX_ANSWER = {
    "session_id": "cx1",
    "cwd": "/",
    "hook_event_name": "PostToolUse",
    "tool_name": "request_user_input",
    "tool_input": {"questions": [{"id": "plan_color", "header": "Plan color", "question": "Red or blue?"}]},
    "tool_response": '{"answers":{"plan_color":{"answers":["Red","ship it, then cut a new branch"]}}}',
}


def _codex_post_tool_use(monkeypatch, payload):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *args: None)
    hook_manager.on_post_tool_use(normalize_payload(dict(payload)))


def test_a_codex_answer_is_recorded_as_the_operators_words(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@a1-1")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    _codex_post_tool_use(monkeypatch, CODEX_ANSWER)
    assert operator_words.matching("master@a1-1", "red", now=time.time()) == "Red, ship it, then cut a new branch"


def test_a_codex_answer_carries_release_and_branch_signals(monkeypatch):
    import hooks.context.branch_guard as branch_guard
    import hooks.context.prod_lockdown as prod_lockdown

    seen = []
    monkeypatch.setattr(prod_lockdown, "set_release_signal", lambda session_id: seen.append(("release", session_id)))
    monkeypatch.setattr(branch_guard, "set_branch_signal", lambda session_id: seen.append(("branch", session_id)))
    _codex_post_tool_use(monkeypatch, CODEX_ANSWER)
    assert seen == [("release", "cx1"), ("branch", "cx1")]


def test_a_recorder_failure_is_logged_and_never_raised(monkeypatch):
    seen = []

    def broken(payload):
        raise RuntimeError("disk full")

    monkeypatch.setattr(operator_words, "typed", broken)
    monkeypatch.setattr(hook_manager, "log", lambda *args: seen.append(args))
    assert hook_manager._operator_words({"prompt": "ship it"}) is False
    assert seen == [("operator words record failed", {"error": "disk full"})]
