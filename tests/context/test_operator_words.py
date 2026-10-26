import json
import time

import pytest

import hooks.config
from hooks import hook_manager
from hooks.context import operator_words
from hooks.targets.normalizer import normalize_payload
from scripts.inbox.wake import WAKE_TEXT
from scripts.swarm.tick import NUDGE

ENV = {"AGENTIHOOKS_AGENT_NAME": "master@a1-1", "AGENTIHOOKS_SWARM": "demo"}
LATER = 100 + operator_words.TTL_SEC


@pytest.fixture(autouse=True)
def signal_state(tmp_path, monkeypatch):
    import hooks.context.branch_guard as branch_guard
    import hooks.context.prod_lockdown as prod_lockdown

    for module in (branch_guard, prod_lockdown):
        monkeypatch.setattr(module, "AGENTIHOOKS_HOME", tmp_path)
        monkeypatch.setattr(module, "get_redis", lambda: None)


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


def test_a_taken_masters_first_typed_prompt_is_recorded_without_swarm_env():
    from hooks.context import master_coding

    master_coding.record_seat("taken", "demo", "master@a1-1")
    payload = {"session_id": "taken", "prompt": "Use the queue for the broker"}
    assert operator_words.typed(payload, {}, now=100)
    assert operator_words.matching("master@a1-1", "use the queue", now=101) == "Use the queue for the broker"
    for prompt in (WAKE_TEXT, NUDGE.format(slug="demo"), "<task-notification> use redis"):
        assert not operator_words.typed({"session_id": "taken", "prompt": prompt}, {}, now=102)
    assert not operator_words.matching("master@a1-1", "use redis", now=103)


def test_a_taken_masters_question_answer_is_recorded_without_swarm_env():
    from hooks.context import master_coding

    master_coding.record_seat("taken", "demo", "master@a1-1")
    answer = {
        "session_id": "taken",
        "tool_name": "AskUserQuestion",
        "tool_response": {"answers": {"question": "Use the queue"}},
    }
    assert operator_words.typed(answer, {}, now=100)
    assert operator_words.matching("master@a1-1", "use the queue", now=101) == "Use the queue"


@pytest.mark.parametrize("launch_swarm", [None, "previous"])
def test_a_taken_master_records_under_its_seat_instead_of_its_launch_identity(launch_swarm):
    from hooks.context import master_coding

    master_coding.record_seat("taken", "demo", "master@a1-1")
    env = {"AGENTIHOOKS_AGENT_NAME": "launch-session"}
    if launch_swarm:
        env["AGENTIHOOKS_SWARM"] = launch_swarm
    assert operator_words.typed({"session_id": "taken", "prompt": "Use the queue"}, env, now=100)
    assert operator_words.matching("master@a1-1", "use the queue", now=101) == "Use the queue"
    assert not operator_words.matching("launch-session", "use the queue", now=101)
    answer = {
        "session_id": "taken",
        "tool_name": "AskUserQuestion",
        "tool_response": {"answers": {"question": "Keep the queue"}},
    }
    assert operator_words.typed(answer, env, now=102)
    assert operator_words.matching("master@a1-1", "keep the queue", now=103) == "Keep the queue"
    assert not operator_words.typed({"session_id": "taken", "prompt": NUDGE.format(slug="demo")}, env, now=104)


@pytest.mark.parametrize("content", [None, "invalid", "[]", "null", "{}"])
def test_an_invalid_taken_seat_records_no_operator_words(content):
    from hooks.context import master_coding

    target = master_coding.seat_path("taken")
    target.parent.mkdir(parents=True)
    if content is not None:
        target.write_text(content)
    assert operator_words.typed({"session_id": "taken", "prompt": "Use the queue"}, {}, now=100)
    assert not operator_words.matching("master@a1-1", "Use the queue", now=101)


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
        operator_words.heard_prompt(f"word{n} was said", ENV, now=100 + n)
    assert ledger_relay.verified("master@a1-1", "word0 was said") == "word0 was said"
    assert ledger_relay.verified("master@a1-1", "word500 was said") == "word500 was said"


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

    from hooks import _redis
    from scripts.swarm.naming import NameRegistry

    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
    redis = _FakeRedis()
    monkeypatch.setattr(
        NameRegistry, "slug_of", lambda self, name: {(redis, "master@abcdef-0001"): "demo"}[(self.redis, name)]
    )
    monkeypatch.setattr(_redis, "get_redis", lambda: redis)
    folder = tmp_path / "operator_words"
    folder.mkdir()
    path = folder / "master@abcdef-0001.json"
    path.write_text(json.dumps({"sessions": [], "rows": [{"at": 100, "words": "Registered old words"}]}))
    assert operator_words.matching("master@abcdef-0001", "Registered old words", now=101) == "Registered old words"
    assert json.loads(path.read_text()) == {"sessions": []}
    operator_words.forget("demo")
    assert operator_words.matching("master@abcdef-0001", "Registered old words", within=None) == ""


def test_upgrade_retries_modern_names_after_the_registry_recovers(tmp_path, monkeypatch):
    import json

    from hooks import _redis
    from scripts.swarm.naming import NameRegistry

    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(_redis, "get_redis", lambda: None)
    folder = tmp_path / "operator_words"
    folder.mkdir()
    path = folder / "master@abcdef-0001.json"
    old = {"sessions": ["old"], "rows": [{"at": 100, "words": "Unresolved words"}]}
    path.write_text(json.dumps(old))
    assert operator_words.recorded("master@abcdef-*") == []
    assert json.loads(path.read_text()) == old
    assert operator_words.heard_prompt(
        "New words", {"AGENTIHOOKS_AGENT_NAME": "master@abcdef-0001", "AGENTIHOOKS_SWARM": "demo"}, now=101
    )
    redis = _FakeRedis()
    monkeypatch.setattr(
        NameRegistry, "slug_of", lambda self, name: {(redis, "master@abcdef-0001"): "demo"}[(self.redis, name)]
    )
    monkeypatch.setattr(_redis, "get_redis", lambda: redis)
    assert operator_words.matching("master@abcdef-0001", "Unresolved words", now=102) == "Unresolved words"
    assert json.loads(path.read_text()) == {"sessions": ["old"]}
    operator_words.forget("demo")
    assert operator_words.recorded("master@abcdef-*") == []
    assert operator_words.matching("master@abcdef-0001", "New words", within=None) == ""
    assert operator_words.matching("master@abcdef-0001", "Unresolved words", within=None) == ""


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


def test_empty_quotes_match_nothing():
    operator_words.record("master@a1-1", "ship it", now=100)
    assert operator_words.matching("master@a1-1", "", now=101) == ""
    assert operator_words.matching("master@a1-1", " \n\t ", now=101) == ""


def test_default_and_absent_swarms_are_recorded_without_swarm_ownership():
    operator_words.record("plain", "default words", now=100)
    operator_words.record("absent", "absent words", now=100, swarm=None)
    operator_words.forget("")
    assert operator_words.recorded("*") == []


def test_question_answers_are_removed_with_their_swarm():
    assert operator_words.heard_answer(
        {"tool_name": "AskUserQuestion", "tool_response": {"answers": {"q": "Swarm answer"}}}, ENV, now=100
    )
    assert operator_words.matching("master@a1-1", "Swarm answer", now=101) == "Swarm answer"
    operator_words.forget("demo")
    assert operator_words.matching("master@a1-1", "Swarm answer", within=None) == ""


def test_upgrade_continues_past_empty_and_unresolved_files(tmp_path, monkeypatch):
    import json

    from hooks import _redis

    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(_redis, "get_redis", lambda: None)
    folder = tmp_path / "operator_words"
    folder.mkdir()
    (folder / "aaa.json").write_text(json.dumps({"sessions": ["first"], "rows": []}))
    (folder / "master@abcdef-0001.json").write_text(json.dumps({"rows": [{"at": 100, "words": "Pending"}]}))
    path = folder / "z-demo-master-1.json"
    path.write_text(json.dumps({"rows": [{"at": 100, "words": "Known"}]}))
    glob = type(folder).glob
    monkeypatch.setattr(type(folder), "glob", lambda self, pattern: iter(sorted(glob(self, pattern))))
    assert operator_words.matching("z-demo-master-1", "Known", now=101) == "Known"
    assert json.loads(path.read_text()) == {"sessions": []}


def test_completed_upgrade_does_not_scan_legacy_files_again(tmp_path, monkeypatch):
    import json

    from hooks import _redis

    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(_redis, "get_redis", lambda: None)
    folder = tmp_path / "operator_words"
    folder.mkdir()
    (folder / "demo-master-1.json").write_text(json.dumps({"rows": [{"at": 100, "words": "Known"}]}))
    (folder / "master@abcdef-0009.json").write_text(json.dumps({"sessions": ["s1"]}))
    assert operator_words.matching("demo-master-1", "Known", now=101) == "Known"

    def no_scan(name):
        raise AssertionError("completed migration scanned legacy files")

    monkeypatch.setattr(operator_words, "_load", no_scan)
    assert operator_words.matching("demo-master-1", "Known", now=101) == "Known"


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
    payload = {
        **CODEX_ANSWER,
        "tool_response": '{"answers":{"plan_color":{"answers":["release dance, then cut a new branch"]}}}',
    }
    _codex_post_tool_use(monkeypatch, payload)
    assert seen == [("release", "cx1"), ("branch", "cx1")]


def test_a_codex_answer_without_release_dance_does_not_arm_publication(monkeypatch):
    import hooks.context.prod_lockdown as prod_lockdown

    seen = []
    monkeypatch.setattr(prod_lockdown, "set_release_signal", lambda session_id: seen.append(session_id))
    _codex_post_tool_use(monkeypatch, CODEX_ANSWER)
    assert seen == []


def test_a_recorder_failure_is_logged_and_never_raised(monkeypatch):
    seen = []

    def broken(payload):
        raise RuntimeError("disk full")

    monkeypatch.setattr(operator_words, "typed", broken)
    monkeypatch.setattr(hook_manager, "log", lambda *args: seen.append(args))
    assert hook_manager._operator_words({"prompt": "ship it"}) is False
    assert seen == [("operator words record failed", {"error": "disk full"})]


class _FakeRedis:
    def __init__(self, get=lambda key: None):
        self.get = get


def _legacy_words(tmp_path, monkeypatch, slug_of, redis=None):
    import json

    from hooks import _redis
    from scripts.swarm import naming

    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(_redis, "get_redis", lambda: redis or _FakeRedis())
    monkeypatch.setattr(naming.NameRegistry, "slug_of", lambda self, name: slug_of(name))
    monkeypatch.setattr(naming, "addresses", lambda name: [name])
    folder = tmp_path / "operator_words"
    folder.mkdir()
    path = folder / "master@abcdef-0001.json"
    path.write_text(json.dumps({"sessions": [], "rows": [{"at": 100, "words": "Old words"}]}))
    return path


def test_a_mid_turn_message_and_an_answer_are_recorded_while_the_registry_times_out(tmp_path, monkeypatch):
    import json

    import redis

    from scripts.swarm_ledger import ledger_relay

    asked = []

    def timeout(name):
        asked.append(name)
        raise redis.exceptions.TimeoutError("Timeout reading from socket")

    path = _legacy_words(tmp_path, monkeypatch, timeout)
    (path.parent / "master@abcdef-0003.json").write_text(json.dumps({"rows": [{"at": 100, "words": "Older words"}]}))
    (path.parent / "demo-master-1.json").write_text(json.dumps({"rows": [{"at": 100, "words": "Known words"}]}))
    env = {"AGENTIHOOKS_AGENT_NAME": "master@abcdef-0002", "AGENTIHOOKS_SWARM": "demo"}
    answer = {"tool_name": "AskUserQuestion", "tool_response": {"answers": {"Freeze?": "Refreeze until push lands"}}}
    assert not operator_words.heard({"prompt": "Launch prompt", "session_id": "s1"}, env, now=100)
    assert operator_words.heard({"prompt": "Freeze the swarm again", "session_id": "s1"}, env, now=101)
    assert operator_words.heard(answer, env, now=102)
    assert ledger_relay.verified("master@abcdef-0002", "freeze the swarm again") == "Freeze the swarm again"
    assert ledger_relay.verified("master@abcdef-0002", "refreeze until push lands") == "Refreeze until push lands"
    asked.clear()
    assert operator_words.matching("demo-master-1", "Known words", within=None) == ""
    assert len(asked) == 1
    assert operator_words.matching("master@abcdef-0001", "Old words", within=None) == ""
    from scripts.swarm import naming

    monkeypatch.setattr(naming.NameRegistry, "slug_of", lambda self, name: "demo")
    assert operator_words.matching("master@abcdef-0001", "Old words", within=None) == "Old words"
    assert operator_words.matching("master@abcdef-0003", "Older words", within=None) == "Older words"
    assert operator_words.matching("demo-master-1", "Known words", within=None) == "Known words"
    operator_words.forget("demo")
    assert operator_words.recorded("*") == []


def test_words_are_recorded_while_the_upgrade_fails(tmp_path, monkeypatch):
    from hooks import _redis

    def broken():
        raise RuntimeError("registry client failed")

    _legacy_words(tmp_path, monkeypatch, lambda name: "demo")
    monkeypatch.setattr(_redis, "get_redis", broken)
    env = {"AGENTIHOOKS_AGENT_NAME": "master@abcdef-0002", "AGENTIHOOKS_SWARM": "demo"}
    answer = {"tool_name": "AskUserQuestion", "tool_response": {"answers": {"Freeze?": "Refreeze until push lands"}}}
    assert not operator_words.heard({"prompt": "Launch prompt", "session_id": "s1"}, env, now=100)
    assert operator_words.heard({"prompt": "Freeze the swarm again", "session_id": "s1"}, env, now=101)
    assert operator_words.heard(answer, env, now=102)
    with operator_words._store(migrate=False) as connection:
        assert connection.execute("SELECT name, swarm, words FROM entries ORDER BY id").fetchall() == [
            ("master@abcdef-0002", "demo", "Freeze the swarm again"),
            ("master@abcdef-0002", "demo", "Refreeze until push lands"),
        ]


def test_a_legacy_name_the_registry_no_longer_knows_completes_the_upgrade(tmp_path, monkeypatch):
    import json

    path = _legacy_words(tmp_path, monkeypatch, lambda name: "")
    assert operator_words.matching("master@abcdef-0001", "Old words", now=101) == "Old words"
    assert json.loads(path.read_text()) == {"sessions": []}

    def no_scan(name):
        raise AssertionError("completed migration scanned legacy files")

    monkeypatch.setattr(operator_words, "_load", no_scan)
    assert operator_words.matching("master@abcdef-0001", "Old words", now=101) == "Old words"


def test_the_upgrade_asks_the_registry_before_locking_the_store(tmp_path, monkeypatch):
    import sqlite3

    def slug_of(name):
        connection = sqlite3.connect(tmp_path / "operator_words" / "words.sqlite3", timeout=0)
        try:
            connection.execute("BEGIN IMMEDIATE")
        finally:
            connection.close()
        return "demo"

    _legacy_words(tmp_path, monkeypatch, slug_of)
    assert operator_words.matching("master@abcdef-0001", "Old words", now=101) == "Old words"


def test_a_failed_alias_lookup_leaves_the_legacy_name_pending(tmp_path, monkeypatch):
    import redis

    from scripts.swarm.naming import NameRegistry

    def timeout(key):
        if key == NameRegistry.key("alias", "master@abcdef-0001"):
            raise redis.exceptions.TimeoutError("Timeout reading from socket")

    _legacy_words(tmp_path, monkeypatch, lambda name: "demo", _FakeRedis(timeout))
    assert operator_words.matching("master@abcdef-0001", "Old words", within=None) == ""
    with operator_words._store(migrate=False) as connection:
        assert connection.execute("SELECT name FROM legacy").fetchall() == []


def test_a_newer_recorded_quote_wins_over_older_migrated_words(tmp_path, monkeypatch):
    _legacy_words(tmp_path, monkeypatch, lambda name: "demo")
    assert operator_words.record("master@abcdef-0001", "OLD  words", now=200, swarm="demo")
    assert operator_words.matching("master@abcdef-0001", "old words", within=None) == "OLD  words"
    assert operator_words.matching("master@abcdef-0001", "old", within=None) == "OLD  words"


@pytest.fixture
def shared(monkeypatch):
    import fakeredis

    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(operator_words, "_shared", lambda: client)
    return client


def _hosted(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_words, "_dir", lambda: tmp_path / "hosted")


def test_swarm_words_reach_a_server_without_the_local_store_and_unowned_words_stay_local(shared, tmp_path, monkeypatch):
    assert operator_words.record("master@a1-1", "Scale the hive  up", now=100, swarm="demo")
    assert operator_words.record("master@a1-1", "Hold the release", now=150, swarm="demo")
    assert operator_words.record("master@b2-1", "Local only words", now=150)
    from scripts.swarm.store import PREFIX

    assert shared.smembers(f"{PREFIX}:operator-words") == {"master@a1-1"}
    assert shared.zcard(f"{PREFIX}:operator-words:master@a1-1:entries") == 2
    _hosted(tmp_path, monkeypatch)
    assert operator_words.matching("master@a1-1", "scale the hive up", now=200) == "Scale the hive  up"
    assert operator_words.matching("master@a1-1", "hold the", now=200) == "Hold the release"
    assert operator_words.matching("master@a1-1", "scale the hive up", now=100 + operator_words.TTL_SEC) == ""
    assert operator_words.matching("master@a1-1", "scale the hive up", now=10**9, within=None) == "Scale the hive  up"
    assert operator_words.matching("master@a1-2", "scale the hive up", now=200) == ""
    assert operator_words.matching("master@b2-1", "local only words", within=None) == ""
    assert operator_words.recorded("master@*") == ["master@a1-1"]


def test_earlier_master_hook_words_upgrade_to_the_hosted_rank_store(shared, tmp_path, monkeypatch):
    from hooks.context import master_coding
    from scripts.swarm_ledger import ledger_relay

    master_coding.record_seat("earlier-master", "demo", "master@abcdef-0001")
    master_coding.record_seat("current-master", "demo", "master@abcdef-0002")
    with monkeypatch.context() as offline:
        offline.setattr(operator_words, "_shared", lambda: None)
        assert hook_manager._operator_words(
            {
                "session_id": "earlier-master",
                "tool_name": "AskUserQuestion",
                "tool_response": {"answers": {"rank": "Mark it urgent"}},
            }
        )
    assert hook_manager._operator_words({"session_id": "current-master", "prompt": "Keep the work moving"})
    assert operator_words.record("master@123456-0001", "Keep it normal", swarm="other")
    _hosted(tmp_path, monkeypatch)
    assert ledger_relay.verified("master@abcdef-0002", "Mark it urgent") == "Mark it urgent"
    assert ledger_relay.verified("master@abcdef-0002", "Keep it normal") == ""
    assert ledger_relay.verified("master@abcdef-0002", "Never said these words") == ""


def test_shared_history_upgrade_retries_and_does_not_duplicate_words(shared, tmp_path, monkeypatch):
    with monkeypatch.context() as offline:
        offline.setattr(operator_words, "_shared", lambda: None)
        operator_words.record("master@a1-1", "Mark it urgent", now=100, swarm="demo")
        operator_words.record("master@b2-1", "Private local words", now=100)
    assert operator_words.matching("master@a1-1", "mark it urgent", within=None)
    assert operator_words.matching("master@a1-1", "mark it urgent", within=None)
    _hosted(tmp_path, monkeypatch)
    assert [row["words"] for row in operator_words._shared_rows("master@a1-1")] == ["Mark it urgent"]
    assert operator_words.matching("master@b2-1", "private local words", within=None) == ""


def test_overlapping_history_publishers_keep_recording_available(shared, monkeypatch):
    with monkeypatch.context() as offline:
        offline.setattr(operator_words, "_shared", lambda: None)
        operator_words.record("master@a1-1", "Mark it urgent", now=100, swarm="demo")
        operator_words.record("master@a1-1", "Keep it high", now=110, swarm="demo")
    mirror = operator_words._mirror
    calls = []

    def overlapping(swarm, name, at, words):
        calls.append(words)
        if len(calls) == 2:
            assert operator_words.record("master@a1-2", "Keep recording words", now=120, swarm="demo")
        return mirror(swarm, name, at, words)

    monkeypatch.setattr(operator_words, "_mirror", overlapping)
    assert operator_words.matching("master@a1-1", "mark it urgent", within=None) == "Mark it urgent"
    with operator_words._store() as connection:
        assert connection.execute("SELECT COUNT(*) FROM published").fetchone() == (3,)
    assert operator_words._shared_rows("master@a1-1") == [
        {"swarm": "demo", "at": 110.0, "words": "Keep it high"},
        {"swarm": "demo", "at": 100.0, "words": "Mark it urgent"},
    ]
    assert operator_words.matching("master@a1-2", "keep recording words", within=None) == "Keep recording words"


def test_partial_history_publication_checkpoints_success_before_retry(shared, monkeypatch):
    with monkeypatch.context() as offline:
        offline.setattr(operator_words, "_shared", lambda: None)
        operator_words.record("master@a1-1", "Mark it urgent", now=100, swarm="demo")
        operator_words.record("master@a1-1", "Keep it high", now=110, swarm="demo")
    mirror = operator_words._mirror
    calls = []

    def interrupted(swarm, name, at, words):
        calls.append(words)
        return False if len(calls) == 2 else mirror(swarm, name, at, words)

    monkeypatch.setattr(operator_words, "_mirror", interrupted)
    with operator_words._store():
        pass
    assert calls == ["Mark it urgent", "Keep it high"]
    with operator_words._store() as connection:
        assert connection.execute("SELECT entry FROM published").fetchall() == [(1,)]
    assert calls == ["Mark it urgent", "Keep it high", "Keep it high"]
    with operator_words._store() as connection:
        assert connection.execute("SELECT COUNT(*) FROM published").fetchone() == (2,)


def test_the_newest_shared_words_win_and_older_words_remain_available(shared, tmp_path, monkeypatch):
    for at, words in ((100, "ship the first build"), (110, "ship the second build"), (120, "ship the third build")):
        operator_words.record("master@a1-1", words, now=at, swarm="demo")
    _hosted(tmp_path, monkeypatch)
    assert operator_words.matching("master@a1-1", "ship the", within=None) == "ship the third build"
    assert operator_words.matching("master@a1-1", "ship the second", within=None) == "ship the second build"
    assert operator_words.matching("master@a1-1", "ship the first", within=None) == "ship the first build"


def test_forget_drops_only_that_swarms_shared_words(shared, tmp_path, monkeypatch):
    operator_words.record("master@a1-1", "merge the other work", now=100, swarm="other")
    operator_words.record("master@a1-1", "merge the demo work", now=110, swarm="demo")
    operator_words.forget("demo")
    _hosted(tmp_path, monkeypatch)
    assert operator_words.matching("master@a1-1", "merge the demo work", within=None) == ""
    assert operator_words.matching("master@a1-1", "merge the other work", within=None) == "merge the other work"


def test_older_shared_rows_match_and_forget_drops_every_copy(shared, tmp_path, monkeypatch):
    operator_words.record("master@a1-1", "merge the first work", now=90, swarm="demo")
    operator_words.record("master@a1-1", "merge the demo work", now=100, swarm="demo")
    operator_words.record("master@a1-1", "merge the demo work", now=100, swarm="demo")
    operator_words.record("master@a1-1", "merge the other work", now=110, swarm="other")
    operator_words.record("master@a1-1", "merge the late work", now=120, swarm="demo")
    _hosted(tmp_path, monkeypatch)
    assert operator_words.matching("master@a1-1", "merge the demo work", within=None) == "merge the demo work"
    operator_words.forget("demo")
    from scripts.swarm.store import PREFIX

    assert shared.zrange(f"{PREFIX}:operator-words:master@a1-1:entries", 0, -1) == [
        json.dumps({"swarm": "other", "at": 110.0, "words": "merge the other work"})
    ]


def test_legacy_shared_lists_remain_readable_and_forgettable(shared, tmp_path, monkeypatch):
    from scripts.swarm.store import PREFIX

    key = f"{PREFIX}:operator-words:master@a1-1"
    shared.sadd(f"{PREFIX}:operator-words", "master@a1-1")
    for at, words in ((100, "Mark it urgent"), (110, "Keep it high"), (120, "Keep it normal")):
        row = json.dumps({"swarm": "demo", "at": at, "words": words})
        shared.lpush(key, row, row, row)
    operator_words.record("master@a1-1", "Mark it normal", now=90, swarm="demo")
    _hosted(tmp_path, monkeypatch)
    assert operator_words.matching("master@a1-1", "mark it", within=None) == "Mark it urgent"
    assert operator_words.matching("master@a1-1", "keep it high", within=None) == "Keep it high"
    operator_words.forget("demo")
    assert operator_words.matching("master@a1-1", "mark it", within=None) == ""
    assert shared.lrange(key, 0, -1) == []
    assert shared.zrange(f"{key}:entries", 0, -1) == []


def test_an_unreachable_or_failing_shared_redis_keeps_the_local_words(tmp_path, monkeypatch):
    import redis

    from scripts.swarm import store

    def refused(environ=None):
        raise redis.exceptions.ConnectionError("refused")

    monkeypatch.setattr(store, "redis_client", refused)
    assert operator_words._shared() is None
    assert operator_words.record("master@a1-1", "keep the local words", now=100, swarm="demo")
    assert operator_words.matching("master@a1-1", "keep the local words", within=None) == "keep the local words"

    class Failing:
        def __getattr__(self, name):
            raise redis.exceptions.ResponseError("NOPERM")

    monkeypatch.setattr(operator_words, "_shared", lambda: Failing())
    assert operator_words.recorded("master@*") == ["master@a1-1"]
    operator_words.forget("demo")
    _hosted(tmp_path, monkeypatch)
    assert operator_words.matching("master@a1-1", "keep the local words", within=None) == ""


def test_the_shared_client_connects_once_per_process_and_retries_after_a_refusal(monkeypatch):
    import redis

    from scripts.swarm import store

    calls = []

    def connect(environ=None):
        calls.append(environ)
        if len(calls) == 1:
            raise redis.exceptions.ConnectionError("refused")
        return "client"

    monkeypatch.setattr(operator_words, "_SHARED", [])
    monkeypatch.setattr(store, "redis_client", connect)
    assert operator_words._shared() is None
    assert operator_words._shared() == "client"
    assert operator_words._shared() == "client"
    assert len(calls) == 2
