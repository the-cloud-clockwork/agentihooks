import json

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
