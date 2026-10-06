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


def test_while_off_the_question_tool_is_refused_and_the_refusal_names_the_ledger_command():
    assert operator_mode.question_block("AskUserQuestion", "s1", SWARM, now=100) == ASK.format(
        slug="demo", name="master@a1-1"
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
    assert str(blocked.value) == ASK.format(slug="demo", name="master@a1-1")


def test_a_failing_question_check_is_logged_and_lets_the_call_through(monkeypatch):
    logged = []
    monkeypatch.setattr(operator_mode, "question_block", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(hook_manager, "log", lambda *a: logged.append(a))
    assert hook_manager._operator_question({"session_id": "s1", "tool_name": "AskUserQuestion"}) == ""
    assert logged == [("operator question check failed", {"error": "disk full"})]
