import json
from pathlib import Path

import pytest

from hooks import hook_manager
from hooks.context import operator_mode
from hooks.context.swarm_heartbeat import is_operator_prompt
from scripts.inbox.wake import WAKE_TEXT
from scripts.swarm import delivery
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord
from scripts.swarm.tick import NUDGE

SWARM = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_AGENT_NAME": "master@a1-1"}
WINDOW = operator_mode.WINDOW_SEC


@pytest.fixture(autouse=True)
def ledgers(tmp_path, monkeypatch):
    path = tmp_path / "ledgers"
    (path / ".sessions").mkdir(parents=True)
    monkeypatch.setenv("LEDGER_DIR", str(path))
    for name in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM_TASK"):
        monkeypatch.delenv(name, raising=False)
    return path


def test_an_unbound_session_always_has_the_operator_present(ledgers):
    env = {"LEDGER_DIR": str(ledgers)}
    assert operator_mode.present("s1", env, now=100)
    assert operator_mode.observe("s1", "operator off", True, now=100) == "off"
    assert operator_mode.present("s1", env, now=101)


def test_a_swarm_or_ledger_bound_session_starts_off_master_included(ledgers):
    assert not operator_mode.present("s1", SWARM, now=100)
    assert not operator_mode.present("s1", {**SWARM, "AGENTIHOOKS_AGENT_NAME": "eng-1@demo"}, now=100)
    (ledgers / ".sessions" / "s2.json").write_text(json.dumps({"slug": "x", "name": "y"}))
    assert not operator_mode.present("s2", {"LEDGER_DIR": str(ledgers)}, now=100)


def test_typed_operator_on_unlocks_only_that_session():
    assert operator_mode.observe("s1", "operator on", True, now=100) == "on"
    assert operator_mode.present("s1", SWARM, now=101)
    assert not operator_mode.present("s2", SWARM, now=101)


def test_a_prompt_that_was_not_typed_never_switches_the_mode():
    assert operator_mode.observe("s1", "operator on", False, now=100) == ""
    assert not operator_mode.present("s1", SWARM, now=101)
    operator_mode.observe("s1", "operator on", True, now=100)
    assert operator_mode.observe("s1", "operator off", False, now=101) == ""
    assert operator_mode.present("s1", SWARM, now=102)


def test_only_a_prompt_that_opens_with_the_words_switches():
    assert operator_mode.switch("Operator  On.") == "on"
    assert operator_mode.switch("operator off, then rebase") == "off"
    for text in ('"operator on" was typed in the master pane', "the operator on call said ship", "operator one", ""):
        assert operator_mode.switch(text) == ""


def test_silence_reverts_the_session_and_each_typed_message_slides_the_window():
    operator_mode.observe("s1", "operator on", True, now=100)
    assert operator_mode.present("s1", SWARM, now=100 + WINDOW - 1)
    assert operator_mode.observe("s1", "rebase the branch", True, now=100 + WINDOW - 1) == ""
    assert operator_mode.present("s1", SWARM, now=100 + 2 * WINDOW - 2)
    assert not operator_mode.present("s1", SWARM, now=100 + 2 * WINDOW - 1)
    operator_mode.observe("s1", "rebase it now", True, now=100 + 2 * WINDOW)
    assert not operator_mode.present("s1", SWARM, now=100 + 2 * WINDOW + 1)


def test_operator_off_ends_it_at_once():
    operator_mode.observe("s1", "operator on", True, now=100)
    assert operator_mode.observe("s1", "operator off", True, now=110) == "off"
    assert not operator_mode.present("s1", SWARM, now=111)
    operator_mode.observe("s1", "keep going", True, now=112)
    assert not operator_mode.present("s1", SWARM, now=113)


def test_an_unreadable_mode_file_holds_the_session_off(tmp_path, monkeypatch):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    (tmp_path / "operator_mode").mkdir()
    (tmp_path / "operator_mode" / "s1.json").write_text("{not json")
    assert not operator_mode.present("s1", SWARM, now=100)
    assert operator_mode.observe("s1", "operator on", True, now=100) == "on"
    assert operator_mode.present("s1", SWARM, now=101)


def test_the_mode_is_kept_per_session_under_the_state_home(tmp_path, monkeypatch):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / "fresh" / "home")
    assert operator_mode.observe("", "operator on", True, now=100) == ""
    assert operator_mode.observe("Pane A/b c", "operator on", True, now=100) == "on"
    assert [p.name for p in (tmp_path / "fresh" / "home" / "operator_mode").iterdir()] == ["Pane_A_b_c.json"]
    assert operator_mode.present("Pane A/b c", SWARM, now=101)


def test_every_prompt_the_swarm_types_into_a_pane_carries_the_mark_and_is_not_typed():
    sent, eng = [], AgentRecord("eng-1@demo", "eng", "t", pane_id="w:p1")
    delivery.HerdrMessenger(herdr=lambda argv: sent.append(argv[-1]) or {}).prompt(eng, WAKE_TEXT)
    HerdrRuntime(herdr=lambda argv: sent.append(argv[-1]) or {}).nudge(eng, NUDGE.format(slug="demo"))
    assert sent == [delivery.marked(WAKE_TEXT), delivery.marked(NUDGE.format(slug="demo"))]
    assert all(text.startswith(delivery.MARK) for text in sent)
    assert not any(is_operator_prompt(text, "demo") for text in sent)
    assert not is_operator_prompt(delivery.marked("operator on"), "demo")


def run_prompt(monkeypatch, capsys, session, prompt):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *a, **k: None)
    hook_manager.on_user_prompt_submit({"session_id": session, "prompt": prompt, "hook_event_name": "UserPromptSubmit"})
    return capsys.readouterr().out


def test_the_prompt_hook_turns_on_only_the_typed_pane_and_never_a_relayed_one(monkeypatch, capsys):
    run_prompt(monkeypatch, capsys, "s1", "You are master@a1-1, the master of swarm demo")
    run_prompt(monkeypatch, capsys, "s2", "operator on is how the operator unlocks you, eng-1@demo")
    run_prompt(monkeypatch, capsys, "s2", delivery.marked("operator on"))
    assert not operator_mode.present("s2", SWARM)
    out = run_prompt(monkeypatch, capsys, "s1", "operator on")
    assert operator_mode.present("s1", SWARM)
    assert not operator_mode.present("s2", SWARM)
    assert operator_mode.ON_NOTICE in out


def test_a_switch_tells_the_session_its_mode_and_any_other_prompt_tells_nothing():
    assert operator_mode.notice({"session_id": "s1", "prompt": "operator on"}, True, now=100) == operator_mode.ON_NOTICE
    assert operator_mode.notice({"session_id": "s1", "prompt": "ship it"}, True, now=101) == ""
    assert (
        operator_mode.notice({"session_id": "s1", "prompt": "operator off"}, True, now=102) == operator_mode.OFF_NOTICE
    )
    assert operator_mode.notice({"prompt": "operator on"}, True, now=103) == ""
    assert operator_mode.notice({"session_id": "s1"}, True, now=104) == ""
    operator_mode.notice({"session_id": "s2", "prompt": "operator on"}, True, now=100)
    assert not operator_mode.present("s2", SWARM, now=100 + WINDOW)


def test_the_hook_injects_the_notice_uncompressed_and_unlogged(monkeypatch):
    injected = []
    monkeypatch.setattr("hooks.common.inject_context", lambda *a, **k: injected.append((a, k)))
    hook_manager._operator_mode({"session_id": "s1", "prompt": "operator on"}, False)
    hook_manager._operator_mode({"session_id": "s1", "prompt": "operator on"}, True)
    assert injected == [((operator_mode.ON_NOTICE,), {"also_log": False, "skip_compression": True})]


def test_a_failing_mode_store_is_logged_and_never_raises_into_the_hook(monkeypatch):
    logged = []
    monkeypatch.setattr(operator_mode, "notice", lambda payload, typed: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(hook_manager, "log", lambda *a: logged.append(a))
    hook_manager._operator_mode({"session_id": "s1", "prompt": "operator on"}, True)
    assert logged == [("operator mode failed", {"error": "disk full"})]


ASK = (
    "The operator is not present in this pane, so the question tool is off. Put the question on the ledger with "
    'agentihooks ledger --slug {slug} --as {name} question add "<the question in plain words>" '
    "and keep working; the master answers it or raises it to the operator."
)
MASTER_ASK = (
    "The operator is not present in this pane, so the question tool is off. Never write the questions as chat text. "
    "Say only one short line, type operator on to answer the questions here, or leave them in Priorities with "
    'agentihooks ledger --slug {slug} --as {name} priority add <item> "<the ask in plain words>".'
)


def test_while_off_the_master_is_told_one_operator_on_line_or_priorities():
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM, now=100) == MASTER_ASK.format(
        slug="demo", name="master@a1-1"
    )


def test_while_off_a_non_master_is_refused_and_the_refusal_names_the_ledger_command():
    env = {**SWARM, "AGENTIHOOKS_AGENT_NAME": "engineer@a1-master@"}
    assert operator_mode.question_block("AskUserQuestion", "s1", env, now=100) == ASK.format(
        slug="demo", name="engineer@a1-master@"
    )


def test_a_ledger_bound_session_is_told_its_own_ledger_and_name(ledgers):
    (ledgers / ".sessions" / "s2.json").write_text(json.dumps({"slug": "work", "name": "eng-2@work"}))
    env = {**SWARM, "LEDGER_DIR": str(ledgers)}
    assert operator_mode.question_block("AskUserQuestion", "s2", env, now=100) == ASK.format(
        slug="work", name="eng-2@work"
    )


def test_an_unreadable_binding_falls_back_to_the_swarm_and_names_what_is_unknown(ledgers):
    (ledgers / ".sessions" / "s4.json").write_text("{not json")
    env = {"AGENTIHOOKS_SWARM": "demo", "LEDGER_DIR": str(ledgers)}
    assert operator_mode.question_block("AskUserQuestion", "s4", env, now=100) == ASK.format(slug="demo", name="<name>")


def test_the_binding_is_read_from_the_home_ledger_folder_by_default():
    sessions = Path.home() / "development-ledger" / ".sessions"
    sessions.mkdir(parents=True)
    (sessions / "s5.json").write_text(json.dumps({"name": "eng-2@work"}))
    assert operator_mode.question_block("AskUserQuestion", "s5", {}, now=100) == ASK.format(
        slug="<slug>", name="eng-2@work"
    )


def test_the_question_tool_passes_while_on_or_unbound_and_other_tools_always_pass(ledgers):
    operator_mode.observe("s1", "operator on", True, now=100)
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM, now=101) == ""
    assert operator_mode.question_block("AskUserQuestion", "s3", {"LEDGER_DIR": str(ledgers)}, now=101) == ""
    assert operator_mode.question_block("Bash", "s3", SWARM, now=101) == ""


def test_the_pre_tool_hook_blocks_the_question_tool_with_the_refusal(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    payload = {"session_id": "s9", "tool_name": "AskUserQuestion", "tool_input": {"questions": []}}
    with pytest.raises(hook_manager.BlockAction) as blocked:
        hook_manager.on_pre_tool_use(payload)
    assert str(blocked.value) == MASTER_ASK.format(slug="demo", name="master@a1-1")


def test_the_pre_tool_hook_blocks_the_codex_question_tool_in_plan_and_default_mode(monkeypatch):
    from hooks.targets.normalizer import normalize_payload

    for key, value in {**SWARM, "AGENTIHOOKS_TARGET": "codex"}.items():
        monkeypatch.setenv(key, value)
    question = {"header": "Plan color", "id": "plan_color", "question": "Red or blue?", "options": []}
    payload = normalize_payload(
        {"session_id": "s9", "tool_name": "request_user_input", "tool_input": {"questions": [question]}}
    )
    with pytest.raises(hook_manager.BlockAction) as blocked:
        hook_manager.on_pre_tool_use(payload)
    assert str(blocked.value) == MASTER_ASK.format(slug="demo", name="master@a1-1")


def test_a_failing_question_check_is_logged_and_lets_the_call_through(monkeypatch):
    logged = []
    monkeypatch.setattr(operator_mode, "question_block", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(hook_manager, "log", lambda *a: logged.append(a))
    assert hook_manager._operator_question({"session_id": "s1", "tool_name": "AskUserQuestion"}) == ""
    assert logged == [("operator question check failed", {"error": "disk full"})]


REMINDER = "Operator not present: replies stay within 20 words."


def test_while_off_a_one_line_reminder_comes_every_second_tool_call():
    seen = [operator_mode.reminder("s1", SWARM, now=100) for _ in range(6)]
    assert seen == ["", REMINDER, "", REMINDER, "", REMINDER]
    assert operator_mode.QUIET_EVERY == 2


def test_the_away_word_limit_defaults_to_twenty_and_reads_the_environment(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_OPERATOR_OFF_MAX_WORDS", raising=False)
    assert operator_mode.reminder("default", SWARM, now=100) == ""
    assert operator_mode.reminder("default", SWARM, now=100) == "Operator not present: replies stay within 20 words."
    monkeypatch.setenv("AGENTIHOOKS_OPERATOR_OFF_MAX_WORDS", "35")
    assert operator_mode.reminder("default", now=100, environ=SWARM) == ""
    assert (
        operator_mode.reminder("default", now=100, environ=SWARM)
        == "Operator not present: replies stay within 20 words."
    )
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    assert operator_mode.reminder("custom", now=100) == ""
    assert operator_mode.reminder("custom", now=100) == "Operator not present: replies stay within 35 words."


def test_every_away_prompt_injects_the_reminder_without_consuming_tool_calls(monkeypatch, capsys):
    expected = "Operator not present: replies stay within 20 words."
    first = run_prompt(monkeypatch, capsys, "s1", delivery.marked("continue"))
    second = run_prompt(monkeypatch, capsys, "s1", delivery.marked("continue"))
    assert first.count(expected) == 1
    assert second.count(expected) == 1
    assert [operator_mode.reminder("s1", SWARM) for _ in range(4)] == ["", expected, "", expected]


def test_prompt_reminders_use_the_configured_limit_uncompressed_and_unlogged(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AGENTIHOOKS_OPERATOR_OFF_MAX_WORDS", "35")
    injected = []
    monkeypatch.setattr("hooks.common.inject_context", lambda *a, **k: injected.append((a, k)))
    hook_manager._operator_mode({"session_id": "s1", "prompt": delivery.marked("continue")}, False)
    assert injected == [
        (("Operator not present: replies stay within 35 words.",), {"also_log": False, "skip_compression": True})
    ]


def test_a_typed_prompt_uncaps_only_its_reply_turn(monkeypatch, capsys):
    expected = "Operator not present: replies stay within 20 words."
    typed_notice = "Operator present for this turn: reply without a word limit."
    run_prompt(monkeypatch, capsys, "s1", "You are master@a1-1, the master of swarm demo")
    run_prompt(monkeypatch, capsys, "s1", delivery.marked("continue"))
    out = run_prompt(monkeypatch, capsys, "s1", "Explain the result")
    assert typed_notice in out
    assert expected not in out
    assert [operator_mode.reminder("s1", SWARM) for _ in range(4)] == [""] * 4
    assert not operator_mode.present("s1", SWARM)
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == ""
    assert [operator_mode.reminder("s2", SWARM) for _ in range(2)] == ["", expected]
    next_turn = run_prompt(monkeypatch, capsys, "s1", delivery.marked("continue"))
    assert expected in next_turn
    assert typed_notice not in next_turn
    assert [operator_mode.reminder("s1", SWARM) for _ in range(2)] == ["", expected]
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == MASTER_ASK.format(
        slug="demo", name="master@a1-1"
    )


def test_a_typed_prompt_opens_the_question_tool_and_a_woken_turn_closes_it(monkeypatch, capsys):
    payload = {"session_id": "s1", "tool_name": "AskUserQuestion", "tool_input": {"questions": []}}
    run_prompt(monkeypatch, capsys, "s1", "You are master@a1-1, the master of swarm demo")
    run_prompt(monkeypatch, capsys, "s1", "triage priorities")
    hook_manager.on_pre_tool_use(payload)
    for woken in (CHANNEL, delivery.marked("continue")):
        run_prompt(monkeypatch, capsys, "s1", woken)
        with pytest.raises(hook_manager.BlockAction) as blocked:
            hook_manager.on_pre_tool_use(payload)
        assert str(blocked.value) == MASTER_ASK.format(slug="demo", name="master@a1-1")


def test_the_question_tool_follows_the_typed_turn_window(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    operator_mode.notice({"session_id": "s1", "prompt": "Explain"}, True, now=100)
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM, now=100 + WINDOW - 1) == ""
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM, now=100 + WINDOW) == MASTER_ASK.format(
        slug="demo", name="master@a1-1"
    )
    assert operator_mode.question_block("AskUserQuestion", "", SWARM, now=101) == MASTER_ASK.format(
        slug="demo", name="master@a1-1"
    )


def test_the_typed_turn_notice_is_exact_and_operator_off_gives_only_the_away_notice(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    expected = "Operator present for this turn: reply without a word limit."
    assert operator_mode.notice({"session_id": "s1", "prompt": "Explain"}, True, now=100) == expected
    assert operator_mode.notice({"session_id": "s1", "prompt": "operator off"}, True, now=101) == (
        "Operator off: the operator is not present in this pane.\n" + REMINDER
    )
    assert [operator_mode.reminder("s1", SWARM, now=102) for _ in range(2)] == ["", REMINDER]
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM, now=102) == MASTER_ASK.format(
        slug="demo", name="master@a1-1"
    )


def test_operator_off_after_on_is_away_at_once_with_reminders_and_no_questions(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    assert operator_mode.notice({"session_id": "s1", "prompt": "operator on"}, True, now=100) == operator_mode.ON_NOTICE
    assert operator_mode.notice({"session_id": "s1", "prompt": "operator off"}, True, now=101) == (
        operator_mode.OFF_NOTICE + "\n" + REMINDER
    )
    assert [operator_mode.reminder("s1", SWARM, now=102) for _ in range(2)] == ["", REMINDER]
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM, now=102) != ""


def test_operator_off_in_an_unbound_session_tells_only_its_mode(ledgers):
    assert operator_mode.notice({"session_id": "s1", "prompt": "operator off"}, True, now=100) == (
        operator_mode.OFF_NOTICE
    )


def test_a_typed_turn_never_outlives_the_presence_deadline(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    operator_mode.notice({"session_id": "s1", "prompt": "operator on"}, True, now=1000)
    assert operator_mode.notice({"session_id": "s1", "prompt": "rebase it"}, True, now=1100) == ""
    assert operator_mode.present("s1", SWARM, now=1100 + WINDOW - 1)
    assert [operator_mode.reminder("s1", SWARM, now=1100 + WINDOW - 1) for _ in range(2)] == ["", ""]
    assert not operator_mode.present("s1", SWARM, now=1100 + WINDOW)
    assert [operator_mode.reminder("s1", SWARM, now=1100 + WINDOW) for _ in range(2)] == ["", REMINDER]


def test_a_typed_turn_while_away_lifts_the_limit_only_inside_the_window(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    typed_notice = "Operator present for this turn: reply without a word limit."
    assert operator_mode.notice({"session_id": "s1", "prompt": "Explain"}, True, now=100) == typed_notice
    assert [operator_mode.reminder("s1", SWARM, now=100 + WINDOW - 1) for _ in range(2)] == ["", ""]
    assert [operator_mode.reminder("s1", SWARM, now=100 + WINDOW) for _ in range(2)] == ["", REMINDER]


CHANNEL = '<channel source="inbox" item_id="ITEM" sender="SENDER" sent_at_ms="MILLIS">Automated notice</channel>'


def test_an_inbox_channel_prompt_is_not_the_operators():
    assert not is_operator_prompt(CHANNEL, "demo")
    assert not is_operator_prompt("\n  " + CHANNEL, "demo")
    assert not is_operator_prompt('<channel source="other">operator on</channel>', "demo")
    assert is_operator_prompt("the channel source is the inbox", "demo")


def test_a_channel_prompt_neither_lifts_the_limit_nor_renews_the_window(monkeypatch, capsys):
    run_prompt(monkeypatch, capsys, "s1", "You are master@a1-1, the master of swarm demo")
    out = run_prompt(monkeypatch, capsys, "s1", CHANNEL)
    assert "Operator present for this turn" not in out
    assert REMINDER in out
    run_prompt(monkeypatch, capsys, "s1", "operator on")
    at = operator_mode._load("s1")["at"]
    run_prompt(monkeypatch, capsys, "s1", CHANNEL)
    assert operator_mode._load("s1")["at"] == at
    assert operator_mode.present("s1", SWARM)
    assert operator_mode.present("s1", SWARM, now=at + WINDOW - 1)
    assert not operator_mode.present("s1", SWARM, now=at + WINDOW)


def test_prompt_and_tool_reminders_respect_the_supplied_presence_clock(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(operator_mode.time, "time", lambda: 5000)
    assert operator_mode.notice({"session_id": "s1", "prompt": "operator on"}, True, now=100) == (
        "Operator on: the operator is present in this pane until thirty minutes after his last typed message."
    )
    assert operator_mode.notice({"session_id": "s1", "prompt": delivery.marked("continue")}, False, now=101) == ""
    assert [operator_mode.reminder("s1", SWARM, now=102) for _ in range(2)] == ["", ""]
    assert (
        operator_mode.notice({"session_id": "s1", "prompt": delivery.marked("continue")}, False, now=100 + WINDOW)
        == REMINDER
    )
    assert [operator_mode.reminder("s1", SWARM, now=100 + WINDOW) for _ in range(2)] == ["", REMINDER]


def test_the_reminder_counts_tool_calls_upward_from_the_first(monkeypatch):
    monkeypatch.setattr(operator_mode, "QUIET_EVERY", 3)
    seen = [operator_mode.reminder("s1", SWARM, now=100) for _ in range(4)]
    assert seen == ["", "", REMINDER, ""]


def test_the_reminder_stays_silent_while_on_unbound_or_without_a_session(ledgers):
    operator_mode.observe("s1", "operator on", True, now=100)
    assert [operator_mode.reminder("s1", SWARM, now=101) for _ in range(3)] == ["", "", ""]
    assert operator_mode.reminder("s2", {"LEDGER_DIR": str(ledgers)}, now=101) == ""
    assert operator_mode.reminder("", SWARM, now=101) == ""
    assert operator_mode.present("s1", SWARM, now=102)


def test_the_reminder_count_survives_a_typed_turn():
    assert operator_mode.reminder("s1", SWARM, now=100) == ""
    operator_mode.observe("s1", "Explain the result", True, now=101)
    assert operator_mode.reminder("s1", SWARM, now=102) == ""
    operator_mode.observe("s1", delivery.marked("continue"), False, now=103)
    assert operator_mode.reminder("s1", SWARM, now=104) == REMINDER


def test_the_pre_tool_hook_injects_the_reminder_uncompressed_and_unlogged(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    injected = []
    monkeypatch.setattr("hooks.common.inject_context", lambda *a, **k: injected.append((a, k)))
    hook_manager._operator_reminder({"session_id": "s1"})
    hook_manager._operator_reminder({"session_id": "s1"})
    assert injected == [((REMINDER,), {"also_log": False, "skip_compression": True})]


def test_every_pre_tool_call_counts_toward_the_reminder(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    injected = []
    monkeypatch.setattr("hooks.common.inject_context", lambda text, *a, **k: injected.append(text))
    for _ in range(3):
        hook_manager.on_pre_tool_use({"session_id": "s7", "tool_name": "Read", "tool_input": {"file_path": "x"}})
    assert injected.count(REMINDER) == 1


@pytest.mark.parametrize("words", [20, 21, 100])
def test_the_stop_hook_never_refuses_a_long_final_message(monkeypatch, words):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    hook_manager.on_stop({"session_id": "s1", "last_assistant_message": "word " * words, "hook_event_name": "Stop"})


def test_a_failing_reminder_is_logged_and_never_raises_into_the_hook(monkeypatch):
    logged = []
    monkeypatch.setattr(operator_mode, "reminder", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(hook_manager, "log", lambda *a: logged.append(a))
    hook_manager._operator_reminder({"session_id": "s1"})
    assert logged == [("operator reminder failed", {"error": "disk full"})]
