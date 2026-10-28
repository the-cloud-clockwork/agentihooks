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
WINDOW = 1800


@pytest.fixture(autouse=True)
def ledgers(tmp_path, monkeypatch):
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda: redis)
    monkeypatch.setattr("scripts.scheduler.service.ensure_running", lambda: True)
    path = tmp_path / "ledgers"
    (path / ".sessions").mkdir(parents=True)
    monkeypatch.setenv("LEDGER_DIR", str(path))
    for name in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM_TASK"):
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
def bound(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")


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


def test_presence_commands_match_sentences_with_command_boundaries():
    assert operator_mode.switch("Operator  On.") == "on"
    assert operator_mode.switch("Please, operator off, then rebase") == "off"
    assert operator_mode.switch("operator on, then operator off") == "off"
    assert operator_mode.switch("operator off, then operator on") == "off"
    for text in ("operator one", "preoperator on", "pre-operator on", "operator on-call", ""):
        assert operator_mode.switch(text) == ""


@pytest.mark.usefixtures("bound")
@pytest.mark.parametrize("word", ["on", "off"])
def test_typed_sentence_switches_only_its_session(word):
    operator_mode.observe("other", "operator on", True, now=100)
    operator_mode.observe("pane", "operator on", True, now=100)
    assert operator_mode.observe("pane", f"Keep working, operator {word}, please.", True, now=110) == word
    assert operator_mode._heard("pane") is (word == "on")
    assert operator_mode._heard("other")


@pytest.mark.usefixtures("bound")
@pytest.mark.parametrize("word", ["on", "off"])
def test_relayed_sentence_does_not_switch_presence(word):
    operator_mode.observe("pane", "operator on", True, now=100)
    assert operator_mode.observe("pane", f"Keep working, operator {word}, please.", False, now=110) == ""
    assert operator_mode._heard("pane")


@pytest.mark.usefixtures("bound")
def test_idle_scheduler_does_not_end_explicit_presence():
    from scripts.inbox.store import InboxStore
    from scripts.scheduler import Scheduler
    from scripts.swarm.store import redis_client

    redis = redis_client()
    operator_mode.observe("pane", "operator on", True, now=100)
    Scheduler(redis).run_due(now=100000)
    assert operator_mode._heard("pane")
    assert operator_mode.question_block("AskUserQuestion", "pane") == ""
    assert InboxStore(redis).inbox("pane") == []
    operator_mode.observe("pane", "operator off", True, now=100001)
    assert not operator_mode._heard("pane")


@pytest.mark.usefixtures("bound")
@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_session_restart_clears_presence_but_compaction_preserves_it(monkeypatch, source):
    from unittest.mock import Mock

    operator_mode.observe("pane", "operator on", True, now=100)
    operator_mode.observe("other", "operator on", True, now=100)
    monkeypatch.setattr(hook_manager, "_inject_refocus", Mock())
    monkeypatch.setattr("hooks.lifecycle.guard.session_event", Mock())
    monkeypatch.setattr(hook_manager, "_run_start_conditions", Mock(side_effect=RuntimeError("stop after reset")))
    with pytest.raises(RuntimeError, match="stop after reset"):
        hook_manager.on_session_start({"session_id": "pane", "source": source})
    assert operator_mode._heard("pane") is (source == "compact")
    assert operator_mode._load("pane")["turn_at"] == (100 if source == "compact" else None)
    assert operator_mode._heard("other")


@pytest.mark.usefixtures("bound")
def test_restart_without_a_session_does_not_change_presence():
    operator_mode.observe("pane", "operator on", True, now=100)
    operator_mode.reset({})
    assert operator_mode._heard("pane")


def test_restart_clears_presence_when_no_source_is_given():
    operator_mode.observe("pane", "operator on", True, now=100)
    operator_mode.reset({"session_id": "pane"})
    assert operator_mode._load("pane") == {"on": False, "at": 100, "turn_at": None}


def test_operator_on_survives_silence_and_later_typed_messages():
    operator_mode.observe("s1", "operator on", True, now=100)
    assert operator_mode.present("s1", SWARM, now=100 + WINDOW - 1)
    assert operator_mode.observe("s1", "rebase the branch", True, now=100 + WINDOW - 1) == ""
    assert operator_mode.present("s1", SWARM, now=100 + 2 * WINDOW - 2)
    assert operator_mode.present("s1", SWARM, now=100 + 2 * WINDOW - 1)
    operator_mode.observe("s1", "rebase it now", True, now=100 + 2 * WINDOW)
    assert operator_mode.present("s1", SWARM, now=100 + 2 * WINDOW + 1)


def test_operator_off_ends_it_at_once():
    operator_mode.observe("s1", "operator on", True, now=100)
    assert operator_mode.observe("s1", "operator off", True, now=110) == "off"
    assert not operator_mode.present("s1", SWARM, now=111)
    operator_mode.observe("s1", "keep going", True, now=112)
    assert not operator_mode._heard("s1", SWARM)
    assert operator_mode.present("s1", SWARM, now=113)


def test_operator_on_keeps_questions_open_through_inbox_and_silence_until_off():
    operator_mode.observe("s1", "operator on", True, now=100)
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == ""
    for prompt in (CHANNEL, delivery.marked("continue")):
        assert not is_operator_prompt(prompt, "demo")
        operator_mode.observe("s1", prompt, False, now=200)
        assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == ""
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == ""
    operator_mode.observe("s1", CHANNEL, False, now=86501)
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == ""
    assert operator_mode.observe("s1", "operator off", True, now=86503) == "off"
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == MASTER_ASK.format(
        slug="demo", name="master@a1-1"
    )


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
    assert [p.name for p in (tmp_path / "fresh" / "home" / "operator_mode").glob("*.json")] == ["Pane_A_b_c.json"]
    assert operator_mode.present("Pane A/b c", SWARM, now=101)


def test_every_prompt_the_swarm_types_into_a_pane_carries_the_mark_and_is_not_typed():
    sent, eng = [], AgentRecord("eng-1@demo", "eng", "t", pane_id="w:p1")
    delivery.HerdrMessenger(herdr=lambda argv: sent.append(argv[-1]) or {}).prompt(eng, WAKE_TEXT)
    HerdrRuntime(herdr=lambda argv: sent.append(argv[-1]) or {}).nudge(eng, NUDGE.format(slug="demo"))
    assert sent == [delivery.marked(WAKE_TEXT), delivery.marked(NUDGE.format(slug="demo"))]
    assert all(text.startswith(delivery.MARK) for text in sent)
    assert not any(is_operator_prompt(text, "demo") for text in sent)
    assert not is_operator_prompt(delivery.marked("operator on"), "demo")


@pytest.mark.parametrize("words", ["operator on", "release dance", "approved merge the pull request"])
def test_verified_peer_delivery_never_switches_or_renews_operator_presence(words):
    from scripts.inbox.store import Item

    item = Item("peer-item", "peer", "master", words, "pending", 100, 100, task="task-one")
    prompt = delivery.envelope(item, "delivery-one", "master")
    typed = is_operator_prompt(prompt, "demo")
    assert not typed
    assert operator_mode.observe("s1", prompt, typed, now=100) == ""
    assert not operator_mode.present("s1", SWARM, now=101)
    operator_mode.observe("s1", "operator on", True, now=102)
    assert operator_mode.observe("s1", prompt, typed, now=103) == ""
    assert operator_mode.present("s1", SWARM, now=102 + WINDOW - 1)
    assert operator_mode.present("s1", SWARM, now=102 + WINDOW)


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


PEER_MESSAGES = (
    'Another Claude session sent a message:\n<cross-session-message from="uds:/run/user/1000/cc-socks/1.sock" '
    'from-name="master-watchdog" from-mode="bypass">\n{words}\n</cross-session-message>',
    'Another Claude session sent a message:\n<agent-message from="local-commits-publisher">\n{words}\n</agent-message>',
    '<cross-session-message from="uds:/run/user/1000/cc-socks/1.sock" from-name="watchdog">\n{words}\n</cross-session-message>',
    '<agent-message from="watchdog">\n{words}\n</agent-message>',
)


@pytest.mark.parametrize("peer", PEER_MESSAGES)
def test_a_cross_session_message_never_switches_presence_or_uncaps_the_turn(monkeypatch, capsys, peer):
    assert not is_operator_prompt(peer.format(words="operator on"), "demo")
    run_prompt(monkeypatch, capsys, "s1", "You are master@a1-1, the master of swarm demo")
    assert operator_mode.ON_NOTICE in run_prompt(monkeypatch, capsys, "s1", "operator on")
    out = run_prompt(monkeypatch, capsys, "s1", peer.format(words="operator off"))
    assert operator_mode.present("s1", SWARM)
    assert operator_mode.OFF_NOTICE not in out
    assert operator_mode.OFF_NOTICE in run_prompt(monkeypatch, capsys, "s1", "operator off")
    out = run_prompt(monkeypatch, capsys, "s1", peer.format(words="operator on"))
    assert not operator_mode.present("s1", SWARM)
    assert operator_mode.ON_NOTICE not in out
    assert "Operator present for this turn" not in out
    assert operator_mode.ON_NOTICE in run_prompt(monkeypatch, capsys, "s1", "operator on")
    assert operator_mode.present("s1", SWARM)


def test_a_switch_tells_the_session_its_mode_and_any_other_prompt_tells_nothing():
    assert operator_mode.notice({"session_id": "s1", "prompt": "operator on"}, True, now=100) == operator_mode.ON_NOTICE
    assert operator_mode.notice({"session_id": "s1", "prompt": "ship it"}, True, now=101) == ""
    assert (
        operator_mode.notice({"session_id": "s1", "prompt": "operator off"}, True, now=102) == operator_mode.OFF_NOTICE
    )
    assert operator_mode.notice({"prompt": "operator on"}, True, now=103) == ""
    assert operator_mode.notice({"session_id": "s1"}, True, now=104) == ""
    operator_mode.notice({"session_id": "s2", "prompt": "operator on"}, True, now=100)
    assert operator_mode.present("s2", SWARM, now=100 + WINDOW)


def test_the_hook_injects_the_notice_uncompressed_and_unlogged(monkeypatch):
    injected = []
    monkeypatch.setattr("hooks.common.inject_context", lambda *a, **k: injected.append((a, k)))
    hook_manager._operator_mode({"session_id": "s1", "prompt": "operator on"}, False)
    hook_manager._operator_mode({"session_id": "s1", "prompt": "operator on"}, True)
    assert injected == [((operator_mode.ON_NOTICE,), {"also_log": False, "skip_compression": True, "kind": "alert"})]


def test_a_failing_mode_store_is_logged_and_never_raises_into_the_hook(monkeypatch):
    logged = []
    monkeypatch.setattr(operator_mode, "notice", lambda payload, typed: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(hook_manager, "log", lambda *a: logged.append(a))
    hook_manager._operator_mode({"session_id": "s1", "prompt": "operator on"}, True)
    assert logged == [("operator mode failed", {"error": "disk full"})]


ASK = (
    "The operator is not present in this pane, so the question tool is off. Send the question to the front door "
    'master by inbox with agentihooks msg send master@{slug} "<the question in plain words>" and keep working.'
)
MASTER_ASK = (
    "The operator is not present in this pane, so the question tool is off. Never write to him first, as chat text "
    "or anywhere else: hold the question until he writes, then answer what he asks."
)
PROMPT_REFUSAL = (
    "Denied {tool}: the operator is not present in this pane, and only his typed operator on opens this prompt. "
    "Keep working without it."
)


def test_while_off_the_master_is_told_one_operator_on_line_or_priorities():
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == MASTER_ASK.format(
        slug="demo", name="master@a1-1"
    )


def test_while_off_a_non_master_is_refused_and_the_refusal_names_the_ledger_command():
    env = {**SWARM, "AGENTIHOOKS_AGENT_NAME": "engineer@a1-master@"}
    assert operator_mode.question_block("AskUserQuestion", "s1", env) == ASK.format(
        slug="demo", name="engineer@a1-master@"
    )


def test_a_ledger_bound_session_is_told_its_own_ledger_and_name(ledgers):
    (ledgers / ".sessions" / "s2.json").write_text(json.dumps({"slug": "work", "name": "eng-2@work"}))
    env = {**SWARM, "LEDGER_DIR": str(ledgers)}
    assert operator_mode.question_block("AskUserQuestion", "s2", env) == ASK.format(slug="work", name="eng-2@work")


def test_an_unreadable_binding_falls_back_to_the_swarm_and_names_what_is_unknown(ledgers):
    (ledgers / ".sessions" / "s4.json").write_text("{not json")
    env = {"AGENTIHOOKS_SWARM": "demo", "LEDGER_DIR": str(ledgers)}
    assert operator_mode.question_block("AskUserQuestion", "s4", env) == ASK.format(slug="demo", name="<name>")


def test_the_binding_is_read_from_the_home_ledger_folder_by_default():
    sessions = Path.home() / "development-ledger" / ".sessions"
    sessions.mkdir(parents=True)
    (sessions / "s5.json").write_text(json.dumps({"name": "eng-2@work"}))
    assert operator_mode.question_block("AskUserQuestion", "s5", {}) == ASK.format(slug="<slug>", name="eng-2@work")


def test_the_question_tool_passes_a_bound_session_only_after_typed_operator_on_and_a_standalone_one_always(ledgers):
    standalone = {"LEDGER_DIR": str(ledgers)}
    assert operator_mode.question_block("AskUserQuestion", "s3", standalone) == ""
    assert operator_mode.question_block("AskUserQuestion", "", standalone) == ""
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) != ""
    operator_mode.observe("s1", "operator on", True, now=100)
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == ""
    operator_mode.observe("s3", "operator off", True, now=100)
    assert operator_mode.question_block("AskUserQuestion", "s3", standalone) == ""
    assert operator_mode.question_block("Bash", "s4", SWARM) == ""
    assert operator_mode.question_block("Bash", "s4", standalone) == ""


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


def test_a_standalone_codex_question_passes_without_operator_on(monkeypatch):
    from hooks.targets.normalizer import normalize_payload

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    question = {"header": "Plan color", "id": "plan_color", "question": "Red or blue?", "options": []}
    payload = normalize_payload(
        {"session_id": "s8", "tool_name": "request_user_input", "tool_input": {"questions": [question]}}
    )
    hook_manager.on_pre_tool_use(payload)
    for prompt in ("Explain the plan", "operator off"):
        operator_mode.observe("s8", prompt, True, now=100)
        hook_manager.on_pre_tool_use(payload)


def test_a_standalone_claude_question_passes_without_operator_on():
    payload = {"session_id": "s7", "tool_name": "AskUserQuestion", "tool_input": {"questions": []}}
    hook_manager.on_pre_tool_use(payload)
    for prompt in ("Explain the plan", "operator off"):
        operator_mode.observe("s7", prompt, True, now=100)
        hook_manager.on_pre_tool_use(payload)


def test_a_failing_question_check_is_logged_and_refuses_only_the_question_tool(monkeypatch):
    logged = []
    monkeypatch.setattr(operator_mode, "question_block", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(hook_manager, "log", lambda *a: logged.append(a))
    assert hook_manager._operator_question({"session_id": "s1", "tool_name": "AskUserQuestion"}) == (
        "The question tool is off: the operator presence check failed. Keep working without it."
    )
    assert hook_manager._operator_question({"session_id": "s1", "tool_name": "Bash"}) == ""
    assert logged == [("operator question check failed", {"error": "disk full"})] * 2


@pytest.mark.parametrize("saved", ["[]", "null", '"on"', "true"])
def test_a_mode_file_that_is_not_an_object_holds_the_question_tool_off(tmp_path, monkeypatch, saved):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    (tmp_path / "operator_mode").mkdir()
    (tmp_path / "operator_mode" / "s1.json").write_text(saved)
    assert operator_mode._load("s1") == {}
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == MASTER_ASK.format(
        slug="demo", name="master@a1-1"
    )


AWAY = (
    "Operator not present: he never reads this pane. End the turn with no report, summary or ask addressed to him "
    "and skip the response template; any pane text stays within {words} words. "
    "Never write to him first; answer only what he writes."
)
REMINDER = AWAY.format(words=20)


def test_while_off_a_one_line_reminder_comes_every_second_tool_call():
    seen = [operator_mode.reminder("s1", SWARM, now=100) for _ in range(6)]
    assert seen == ["", REMINDER, "", REMINDER, "", REMINDER]
    assert operator_mode.QUIET_EVERY == 2


def test_the_away_word_limit_defaults_to_twenty_and_reads_the_environment(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_OPERATOR_OFF_MAX_WORDS", raising=False)
    assert operator_mode.reminder("default", SWARM, now=100) == ""
    assert operator_mode.reminder("default", SWARM, now=100) == REMINDER
    monkeypatch.setenv("AGENTIHOOKS_OPERATOR_OFF_MAX_WORDS", "35")
    assert operator_mode.reminder("default", now=100, environ=SWARM) == ""
    assert operator_mode.reminder("default", now=100, environ=SWARM) == REMINDER
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    assert operator_mode.reminder("custom", now=100) == ""
    assert operator_mode.reminder("custom", now=100) == AWAY.format(words=35)


def test_every_away_prompt_injects_the_reminder_without_consuming_tool_calls(monkeypatch, capsys):
    expected = REMINDER
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
        (
            (AWAY.format(words=35),),
            {"also_log": False, "skip_compression": True, "kind": "alert"},
        )
    ]


def test_a_typed_master_prompt_lifts_reply_limit_but_keeps_questions_locked(monkeypatch, capsys):
    run_prompt(monkeypatch, capsys, "s1", "You are master@a1-1, the master of swarm demo")
    out = run_prompt(monkeypatch, capsys, "s1", "Explain the result")
    assert "Operator present in this pane during the quiet window. Answer his message." in out
    assert REMINDER not in out
    assert [operator_mode.reminder("s1", SWARM) for _ in range(2)] == ["", ""]
    assert operator_mode.present("s1", SWARM)
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == MASTER_ASK.format(
        slug="demo", name="master@a1-1"
    )


def test_away_turns_end_without_a_report_and_a_typed_master_prompt_restores_replies(monkeypatch, capsys):
    run_prompt(monkeypatch, capsys, "s1", "You are master@a1-1, the master of swarm demo")
    away = run_prompt(monkeypatch, capsys, "s1", delivery.marked("continue"))
    assert "End the turn with no report, summary or ask addressed to him" in away
    assert "Never write to him first; answer only what he writes." in away
    typed = run_prompt(monkeypatch, capsys, "s1", "Explain the result")
    assert "Operator present in this pane during the quiet window. Answer his message." in typed
    present = run_prompt(monkeypatch, capsys, "s1", "operator on")
    assert "addressed to him" not in present
    assert "only in Priorities" not in present


def test_an_away_worker_routes_his_decisions_to_the_front_door_inbox_never_the_ledger(ledgers):
    env = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_AGENT_NAME": "engineer@a1-2", "LEDGER_DIR": str(ledgers)}
    text = [operator_mode.reminder("w1", env, now=100) for _ in range(2)][1]
    assert "End the turn with no report, summary or ask addressed to him" in text
    assert "front door master by inbox with agentihooks msg send" in text
    assert "question add" not in text
    assert "Priorities" not in text


def test_the_session_binding_picks_the_route_over_the_environment_name(ledgers, monkeypatch, capsys):
    (ledgers / ".sessions" / "w1.json").write_text(json.dumps({"slug": "demo", "name": "engineer@a1-2"}))
    (ledgers / ".sessions" / "m1.json").write_text(json.dumps({"slug": "demo", "name": "master@a1-1"}))
    out = run_prompt(monkeypatch, capsys, "w1", delivery.marked("continue"))
    assert operator_mode.WORKER_ROUTE in out
    assert operator_mode.MASTER_ROUTE not in out
    env = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_AGENT_NAME": "engineer@a1-2", "LEDGER_DIR": str(ledgers)}
    text = [operator_mode.reminder("m1", env, now=100) for _ in range(2)][1]
    assert text.endswith(operator_mode.MASTER_ROUTE)


def test_only_typed_operator_on_opens_the_question_tool_and_no_other_prompt_does(monkeypatch, capsys):
    payload = {"session_id": "s1", "tool_name": "AskUserQuestion", "tool_input": {"questions": []}}
    run_prompt(monkeypatch, capsys, "s1", "You are master@a1-1, the master of swarm demo")
    for prompt in ("triage priorities", CHANNEL, delivery.marked("operator on")):
        run_prompt(monkeypatch, capsys, "s1", prompt)
        with pytest.raises(hook_manager.BlockAction) as blocked:
            hook_manager.on_pre_tool_use(payload)
        assert str(blocked.value) == MASTER_ASK.format(slug="demo", name="master@a1-1")
    run_prompt(monkeypatch, capsys, "s1", "operator on")
    hook_manager.on_pre_tool_use(payload)


def test_the_typed_turn_window_never_opens_the_question_tool(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    operator_mode.notice({"session_id": "s1", "prompt": "Explain"}, True, now=100)
    assert operator_mode._load("s1")["turn_at"] == 100
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == MASTER_ASK.format(
        slug="demo", name="master@a1-1"
    )
    assert operator_mode.question_block("AskUserQuestion", "", SWARM) == MASTER_ASK.format(
        slug="demo", name="master@a1-1"
    )


def test_a_typed_master_prompt_gets_presence_notice_and_operator_off_the_off_notice(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    assert operator_mode.notice({"session_id": "s1", "prompt": "Explain"}, True, now=100) == (
        "Operator present in this pane during the quiet window. Answer his message."
    )
    assert operator_mode.notice({"session_id": "s1", "prompt": "operator off"}, True, now=101) == (
        "Operator off: the operator is not present in this pane.\n" + REMINDER
    )
    assert [operator_mode.reminder("s1", SWARM, now=102) for _ in range(2)] == ["", REMINDER]
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) == MASTER_ASK.format(
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
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM) != ""


def test_operator_off_in_an_unbound_session_tells_only_its_mode(ledgers):
    assert operator_mode.notice({"session_id": "s1", "prompt": "operator off"}, True, now=100) == (
        operator_mode.OFF_NOTICE
    )


def test_a_typed_turn_does_not_expire_explicit_operator_presence(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    operator_mode.notice({"session_id": "s1", "prompt": "operator on"}, True, now=1000)
    assert operator_mode.notice({"session_id": "s1", "prompt": "rebase it"}, True, now=1100) == ""
    assert operator_mode.present("s1", SWARM, now=1100 + WINDOW - 1)
    assert [operator_mode.reminder("s1", SWARM, now=1100 + WINDOW - 1) for _ in range(2)] == ["", ""]
    assert operator_mode.present("s1", SWARM, now=1100 + WINDOW)
    assert [operator_mode.reminder("s1", SWARM, now=1100 + WINDOW) for _ in range(2)] == ["", ""]


def test_a_typed_worker_turn_while_away_never_lifts_the_limit(monkeypatch):
    env = {**SWARM, "AGENTIHOOKS_AGENT_NAME": "engineer@a1-2"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert operator_mode.notice({"session_id": "s1", "prompt": "Explain"}, True, now=100) == (
        "Operator not present: he never reads this pane. End the turn with no report, summary or ask addressed to him "
        "and skip the response template; any pane text stays within 20 words. "
        "Never write to him: send anything he must decide to the front door master by inbox with agentihooks msg send."
    )
    assert not operator_mode.present("s1", env, now=101)
    assert operator_mode.reply_refusal({"session_id": "s1", "last_assistant_message": "word " * 21}, env) != ""


@pytest.mark.parametrize("prompt", ["Explain the current work", "What is waiting?"])
def test_master_pane_prompt_allows_long_reply_only_during_quiet_window(monkeypatch, capsys, prompt):
    monkeypatch.setenv("AGENTIHOOKS_INBOX_QUIET_S", "180")
    monkeypatch.setattr(operator_mode.time, "time", lambda: 100)
    run_prompt(monkeypatch, capsys, "master-pane", "You are master@a1-1, the master of swarm demo")
    out = run_prompt(monkeypatch, capsys, "master-pane", prompt)
    assert REMINDER not in out
    payload = {"session_id": "master-pane", "last_assistant_message": "word " * 21}
    assert operator_mode.present("master-pane", SWARM, now=279)
    assert not operator_mode.present("other-pane", SWARM, now=101)
    assert operator_mode.reply_refusal(payload, SWARM) == ""
    monkeypatch.setattr(operator_mode.time, "time", lambda: 280)
    assert not operator_mode.present("master-pane", SWARM, now=280)
    assert operator_mode.reply_refusal(payload, SWARM)


@pytest.mark.parametrize(
    "prompt", ['<channel source="inbox">operator on</channel>', delivery.marked("operator on"), WAKE_TEXT]
)
def test_automated_master_prompts_neither_grant_nor_renew_temporary_presence(monkeypatch, capsys, prompt):
    monkeypatch.setenv("AGENTIHOOKS_INBOX_QUIET_S", "10")
    monkeypatch.setattr(operator_mode.time, "time", lambda: 100)
    run_prompt(monkeypatch, capsys, "pane", "You are master@a1-1, the master of swarm demo")
    run_prompt(monkeypatch, capsys, "pane", prompt)
    assert not operator_mode.present("pane", SWARM, now=100)
    run_prompt(monkeypatch, capsys, "pane", "Explain the work")
    assert operator_mode._load("pane")["pane_at"] == 100
    monkeypatch.setattr(operator_mode.time, "time", lambda: 109)
    run_prompt(monkeypatch, capsys, "pane", prompt)
    assert operator_mode.present("pane", now=109)
    assert operator_mode._load("pane")["pane_at"] == 100
    assert not operator_mode.present("pane", now=110)


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_master_temporary_presence_resets_on_restart_and_survives_compaction(monkeypatch, source):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    operator_mode.observe("pane", "Explain the work", True, now=100)
    operator_mode.reset({"session_id": "pane", "source": source})
    assert operator_mode.present("pane", SWARM, now=101) is (source == "compact")


def test_master_temporary_presence_renews_only_on_operator_words_and_off_clears_it(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    operator_mode.observe("pane", "Explain the work", True, now=100)
    assert not operator_mode.present("pane", SWARM, now=99)
    assert operator_mode.present("pane", SWARM, now=279)
    assert not operator_mode.present("pane", SWARM, now=280)
    operator_mode.observe("pane", "What is next?", True, now=200)
    assert operator_mode._load("pane")["pane_at"] == 200
    assert operator_mode.present("pane", SWARM, now=379)
    assert not operator_mode.present("pane", SWARM, now=380)
    operator_mode.observe("pane", "operator off", True, now=201)
    assert "pane_at" not in operator_mode._load("pane")
    assert not operator_mode.present("pane", SWARM, now=201)


def test_master_temporary_presence_uses_the_session_binding_and_supplied_environment(ledgers):
    (ledgers / ".sessions" / "pane.json").write_text(json.dumps({"slug": "demo", "name": "master@a1-1"}))
    operator_mode.observe("pane", "Explain the work", True, now=100)
    env = {"LEDGER_DIR": str(ledgers), "AGENTIHOOKS_AGENT_NAME": "engineer@a1-2", "AGENTIHOOKS_INBOX_QUIET_S": "2"}
    assert operator_mode.present("pane", env, now=101)
    assert not operator_mode.present("pane", env, now=102)
    (ledgers / ".sessions" / "pane.json").write_text(json.dumps({"slug": "demo", "name": "engineer@a1-2"}))
    assert not operator_mode.present("pane", {**env, **SWARM}, now=101)


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
    assert operator_mode.present("s1", SWARM, now=at + WINDOW)


def test_explicit_presence_keeps_reminders_off_past_the_supplied_clock(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(operator_mode.time, "time", lambda: 5000)
    assert operator_mode.notice({"session_id": "s1", "prompt": "operator on"}, True, now=100) == (
        "Operator on: the operator is present in this pane until he types operator off or the session restarts."
    )
    assert operator_mode.notice({"session_id": "s1", "prompt": delivery.marked("continue")}, False, now=101) == ""
    assert [operator_mode.reminder("s1", SWARM, now=102) for _ in range(2)] == ["", ""]
    assert (
        operator_mode.notice({"session_id": "s1", "prompt": delivery.marked("continue")}, False, now=100 + WINDOW) == ""
    )
    assert [operator_mode.reminder("s1", SWARM, now=100 + WINDOW) for _ in range(2)] == ["", ""]


def test_the_reminder_counts_tool_calls_upward_from_the_first(monkeypatch):
    monkeypatch.setattr(operator_mode, "QUIET_EVERY", 3)
    seen = [operator_mode.reminder("s1", SWARM, now=100) for _ in range(4)]
    assert seen == ["", "", REMINDER, ""]
    assert operator_mode._load("s1")["calls"] == 4


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
    assert operator_mode.reminder("s1", SWARM, now=281) == REMINDER
    assert operator_mode.reminder("s1", SWARM, now=282) == ""


def test_the_pre_tool_hook_injects_the_reminder_uncompressed_and_unlogged(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    injected = []
    monkeypatch.setattr("hooks.common.inject_context", lambda *a, **k: injected.append((a, k)))
    hook_manager._operator_reminder({"session_id": "s1"})
    hook_manager._operator_reminder({"session_id": "s1"})
    assert injected == [((REMINDER,), {"also_log": False, "skip_compression": True, "kind": "alert"})]


def test_every_pre_tool_call_counts_toward_the_reminder(monkeypatch):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    injected = []
    monkeypatch.setattr("hooks.common.inject_context", lambda text, *a, **k: injected.append(text))
    for _ in range(3):
        hook_manager.on_pre_tool_use({"session_id": "s7", "tool_name": "Read", "tool_input": {"file_path": "x"}})
    assert injected.count(REMINDER) == 1


@pytest.mark.parametrize("words", [20, 21, 100])
def test_the_stop_hook_refuses_an_over_limit_reply_while_off(monkeypatch, words):
    for key, value in SWARM.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *a, **k: None)
    payload = {"session_id": "s1", "last_assistant_message": "word " * words, "hook_event_name": "Stop"}
    if words <= 20:
        hook_manager.on_stop(payload)
        return
    with pytest.raises(hook_manager.BlockAction) as blocked:
        hook_manager.on_stop(payload)
    assert str(blocked.value) == (
        f"Operator not present: this reply has {words} words, over the 20 word limit. "
        "Reply again in at most 20 words, machine style: result only, no explanation."
    )


HIVE = {**SWARM, "AGENTIHOOKS_AGENT_NAME": "eng-1@demo", "AGENTIHOOKS_HIVE_ID": "hive-001"}
WORKSTATION = {**SWARM, "AGENTIHOOKS_AGENT_NAME": "eng-1@demo"}
LONG = "word " * 21


@pytest.mark.parametrize("env", [HIVE, WORKSTATION, SWARM], ids=["hive", "workstation", "master"])
def test_every_swarm_agent_starts_off_and_its_long_reply_is_refused(env):
    assert not operator_mode.present("s1", env, now=100)
    assert operator_mode.reply_refusal({"session_id": "s1", "last_assistant_message": LONG}, env) == (
        "Operator not present: this reply has 21 words, over the 20 word limit. "
        "Reply again in at most 20 words, machine style: result only, no explanation."
    )


def test_a_standalone_session_is_never_gated(ledgers):
    env = {"LEDGER_DIR": str(ledgers)}
    assert operator_mode.present("s1", env, now=100)
    assert operator_mode.reply_refusal({"session_id": "s1", "last_assistant_message": LONG}, env) == ""


def test_a_hand_seated_master_without_swarm_env_starts_off_until_operator_on(ledgers, tmp_path, monkeypatch):
    from hooks.context import master_coding

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    env = {"LEDGER_DIR": str(ledgers)}
    master_coding.record_seat("m1", "demo", "master@a1-1")
    assert not operator_mode.present("m1", env, now=100)
    operator_mode.observe("m1", "triage the priorities", True, now=100)
    assert operator_mode.question_block("AskUserQuestion", "m1", env) == MASTER_ASK.format(
        slug="demo", name="master@a1-1"
    )
    assert operator_mode.reply_refusal({"session_id": "m1", "last_assistant_message": LONG}, env) != ""
    operator_mode.observe("m1", "operator on", True, now=101)
    assert operator_mode.question_block("AskUserQuestion", "m1", env) == ""
    assert operator_mode.present("m2", env, now=101)


def test_operator_on_lifts_the_limit_and_operator_off_restores_it():
    operator_mode.observe("s1", "operator on", True, now=100)
    assert operator_mode.reply_refusal({"session_id": "s1", "last_assistant_message": LONG}, WORKSTATION) == ""
    operator_mode.observe("s1", "operator off", True, now=101)
    assert operator_mode.reply_refusal({"session_id": "s1", "last_assistant_message": LONG}, WORKSTATION) != ""


def test_the_reply_limit_reads_the_environment_skips_markers_and_checks_retries():
    env = {**WORKSTATION, "AGENTIHOOKS_OPERATOR_OFF_MAX_WORDS": "3"}
    marker = "<!-- @lesson -->\none two three four\n<!-- @/lesson -->"
    assert operator_mode.reply_refusal({"session_id": "s1", "last_assistant_message": "one two three"}, env) == ""
    assert operator_mode.reply_refusal({"session_id": "s1", "last_assistant_message": "ok " + marker}, env) == ""
    assert operator_mode.reply_refusal({"session_id": "s1", "last_assistant_message": "a b <!-- c d e -->"}, env) == ""
    refused = operator_mode.reply_refusal({"session_id": "s1", "last_assistant_message": "a b c d"}, env)
    assert refused.startswith("Operator not present: this reply has 4 words, over the 3 word limit.")
    repeat = {"session_id": "s1", "last_assistant_message": "a b c d", "stop_hook_active": True}
    assert operator_mode.reply_refusal(repeat, env) == refused
    assert operator_mode.reply_refusal({**repeat, "last_assistant_message": "a b c"}, env) == ""
    assert operator_mode.REPLY_RETRIES == 3
    assert operator_mode.reply_refusal(repeat, env) == refused
    assert operator_mode._load("s1")["refusals"] == 3
    assert operator_mode.reply_refusal(repeat, env) == ""
    assert operator_mode._load("s1")["refusals"] == 4
    assert operator_mode.reply_refusal({**repeat, "stop_hook_active": False}, env) == refused
    assert operator_mode._load("s1")["refusals"] == 1
    assert operator_mode.reply_refusal({**repeat, "session_id": "fresh"}, env) == refused
    assert operator_mode._load("fresh")["refusals"] == 1
    assert operator_mode.reply_refusal({"session_id": "", "last_assistant_message": "a b c d"}, env) == ""
    assert operator_mode.reply_refusal({"session_id": "s1"}, env) == ""


def test_spoken_text_drops_markers_and_comments_in_linear_scan():
    spoken = operator_mode._spoken
    assert spoken("a <!-- @lesson -->x y<!-- @/lesson --> b") == "a    b"
    assert spoken("a <!-- @decision date=1 -->x<!-- note -->y<!-- @/lesson -->z<!-- @/decision -->b") == "a     b"
    assert spoken("a <!-- note -->b") == "a  b"
    assert spoken("a <!-- note -->b @/XXXX") == "a  b @/XXXX"
    assert spoken("a <!-- @lesson -->x y") == "a  x y"
    assert spoken("a <!-- open") == "a  <!-- open"
    assert spoken("x<!-- @a -->y<!-- open") == "x y <!-- open"
    assert spoken("<!-- @/lesson -->a") == " a"
    assert spoken("<!-- x @y -->z<!-- @/y -->w") == " z w"
    assert spoken("<!--@a-->x<!--@/a-->y") == "  y"
    assert spoken("<!-- @a -->x<!-- @/ab -->y<!-- @/a -->z") == "   z"
    assert spoken("<!-- @a -->x<!-- @a -->y<!-- @/a -->z") == "   z"
    assert spoken("a <!-- n -->b-->c") == "a  b-->c"


def test_a_failing_reply_check_is_logged_and_never_raises_into_the_stop_hook(monkeypatch):
    logged = []
    monkeypatch.setattr(operator_mode, "reply_refusal", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(hook_manager, "log", lambda *a, **k: logged.append(a))
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *a, **k: None)
    hook_manager.on_stop({"session_id": "s1", "last_assistant_message": LONG, "hook_event_name": "Stop"})
    assert ("conditions stop failed", {"error": "disk full"}) in logged


def test_a_failing_reminder_is_logged_and_never_raises_into_the_hook(monkeypatch):
    logged = []
    monkeypatch.setattr(operator_mode, "reminder", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(hook_manager, "log", lambda *a: logged.append(a))
    hook_manager._operator_reminder({"session_id": "s1"})
    assert logged == [("operator reminder failed", {"error": "disk full"})]
