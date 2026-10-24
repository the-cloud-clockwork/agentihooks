import json
from pathlib import Path

import pytest

import hooks.context.ledger_decision as decision
from hooks import hook_manager
from hooks.targets.emitter import flush

ROOT = Path(__file__).resolve().parents[2]
RECORDED = json.loads((ROOT / "tests" / "fixtures" / "ledger_decision_payloads.json").read_text(encoding="utf-8"))
TOOLBELT = ROOT / "profiles" / "package" / "rules" / "agentihooks-toolbelt.md"
MARK = "LEDGER DECISION"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    ledgers = tmp_path / "ledgers"
    (ledgers / ".sessions").mkdir(parents=True)
    monkeypatch.setenv("LEDGER_DIR", str(ledgers))
    monkeypatch.setattr(decision, "STATE_DIR", tmp_path / "decision-state")
    for name in ("AGENTIHOOKS_TARGET", "AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK", "AGENTIHOOKS_AGENT_NAME"):
        monkeypatch.delenv(name, raising=False)
    return ledgers


def recorded(name, **changes):
    return {**json.loads(json.dumps(RECORDED[name])), **changes}


def run(payload, capsys, target="claude", monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("AGENTIHOOKS_TARGET", target)
    event = payload["hook_event_name"]
    handler = hook_manager.on_post_tool_use if event == "PostToolUse" else hook_manager.on_user_prompt_submit
    handler(payload)
    flush(event)
    return capsys.readouterr().out


def prompt(text, session="s1"):
    return {"session_id": session, "hook_event_name": "UserPromptSubmit", "prompt": text, "cwd": "/"}


def todos(count, session="s1"):
    items = [{"content": f"step {i}", "status": "pending", "activeForm": f"doing {i}"} for i in range(count)]
    return {
        "session_id": session,
        "hook_event_name": "PostToolUse",
        "tool_name": "TodoWrite",
        "tool_input": {"todos": items},
        "tool_response": {"oldTodos": [], "newTodos": items},
        "cwd": "/",
    }


def bind(ledgers, session, slug="rig-grade-swarm", name="master@323133-0001"):
    (ledgers / ".sessions" / f"{session}.json").write_text(json.dumps({"slug": slug, "name": name}), encoding="utf-8")


def test_an_unbound_claude_session_accepting_a_plan_gets_nothing(capsys, monkeypatch):
    assert MARK not in run(recorded("claude_plan_accept"), capsys, "claude", monkeypatch)


def test_an_unbound_codex_session_accepting_a_plan_gets_nothing(capsys, monkeypatch):
    assert MARK not in run(recorded("codex_plan_accept"), capsys, "codex", monkeypatch)


def test_an_unbound_copilot_session_leaving_plan_mode_gets_nothing(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "copilot")
    payload = {
        "session_id": "c1",
        "hook_event_name": "PostToolUse",
        "tool_name": "exit_plan_mode",
        "tool_input": {"summary": "Write, run and delete hello.py"},
    }
    assert decision.directive(payload) == ""


@pytest.mark.parametrize("count", [4, 5, 12])
def test_an_unbound_session_with_a_four_item_task_list_gets_nothing(count, capsys, monkeypatch):
    assert MARK not in run(todos(count), capsys, "claude", monkeypatch)


@pytest.mark.parametrize("count", [4, 8])
def test_an_unbound_session_adding_four_tasks_gets_nothing(count, capsys, monkeypatch):
    for i in range(count):
        payload = {
            "session_id": "s1",
            "hook_event_name": "PostToolUse",
            "tool_name": "TaskCreate",
            "tool_input": {"subject": f"step {i}", "description": f"do step {i}"},
            "tool_response": {"task": {"id": str(i)}},
            "cwd": "/",
        }
        assert MARK not in run(payload, capsys, "claude", monkeypatch)


TROUBLESHOOT_PROMPTS = [
    "troubleshoot why the ledger page is blank",
    "Debug the failing shard",
    "investigate the slow Redis test",
    "refactor the inbox module",
    "we have a bug, start debugging it",
    "Troubleshooting the deploy rollout",
    "an investigation into the memory leak",
    "please help me debug this",
]


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.parametrize("text", TROUBLESHOOT_PROMPTS)
def test_an_unbound_session_asked_to_troubleshoot_gets_nothing(text, target, capsys, monkeypatch):
    assert MARK not in run(prompt(text), capsys, target, monkeypatch)


def test_an_unbound_trivial_prompt_gets_nothing(capsys, monkeypatch):
    assert MARK not in run(prompt("rename the helper to build_block"), capsys, "claude", monkeypatch)


def test_a_codex_prompt_naming_a_plan_but_not_accepting_one_gets_nothing(capsys, monkeypatch):
    assert MARK not in run(recorded("codex_plan_prompt"), capsys, "codex", monkeypatch)


def test_a_non_plan_tool_call_on_an_unbound_session_gets_nothing(capsys, monkeypatch):
    payload = {
        "session_id": "s1",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
        "cwd": "/",
    }
    assert MARK not in run(payload, capsys, "claude", monkeypatch)


def test_a_swarm_agent_appends_its_plan_to_the_swarm_ledger(capsys, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "swarm-buildout")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@323133-1")
    out = run(recorded("codex_plan_accept"), capsys, "codex", monkeypatch)
    assert MARK in out
    assert "agentihooks ledger --slug swarm-buildout --as master@323133-1 plan phases" in out
    assert "init-swarm" not in out


def test_a_session_bound_to_a_ledger_appends_its_plan_there(isolated, capsys, monkeypatch):
    bind(isolated, RECORDED["claude_plan_accept"]["session_id"], slug="rig-grade-swarm", name="master@323133-0001")
    out = run(recorded("claude_plan_accept"), capsys, "claude", monkeypatch)
    assert MARK in out
    assert "agentihooks ledger --slug rig-grade-swarm --as master@323133-0001 plan phases <phases.json>" in out
    assert "without asking" in out


@pytest.mark.parametrize("count", [4, 12])
def test_a_bound_session_with_a_four_item_task_list_still_gets_nothing(count, isolated, capsys, monkeypatch):
    bind(isolated, "s1")
    assert MARK not in run(todos(count), capsys, "claude", monkeypatch)


@pytest.mark.parametrize("text", TROUBLESHOOT_PROMPTS)
def test_a_bound_session_asked_to_troubleshoot_still_gets_nothing(text, isolated, capsys, monkeypatch):
    bind(isolated, "s1")
    assert MARK not in run(prompt(text), capsys, "claude", monkeypatch)


def test_the_bound_directive_fires_once_per_session(isolated, capsys, monkeypatch):
    bind(isolated, "s1")
    assert MARK in run(recorded("claude_plan_accept", session_id="s1"), capsys, "claude", monkeypatch)
    assert MARK not in run(recorded("claude_plan_accept", session_id="s1"), capsys, "claude", monkeypatch)


def test_another_bound_session_gets_its_own_directive(isolated, capsys, monkeypatch):
    bind(isolated, RECORDED["claude_plan_accept"]["session_id"])
    bind(isolated, "other-session")
    assert MARK in run(recorded("claude_plan_accept"), capsys, "claude", monkeypatch)
    assert MARK in run(recorded("claude_plan_accept", session_id="other-session"), capsys, "claude", monkeypatch)


def test_the_acceptance_signal_follows_the_harness(isolated, monkeypatch):
    bind(isolated, "a")
    bind(isolated, "b")
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    assert decision.directive(recorded("codex_plan_accept", session_id="a")) == ""
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    assert decision.directive(recorded("claude_plan_accept", session_id="b")) == ""


def test_a_plan_without_a_session_id_gets_nothing(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    assert decision.directive(recorded("claude_plan_accept", session_id="")) == ""


def test_the_binding_defaults_to_the_home_ledger_folder(capsys, monkeypatch):
    monkeypatch.delenv("LEDGER_DIR")
    sessions = Path.home() / "development-ledger" / ".sessions"
    sessions.mkdir(parents=True)
    (sessions / "s1.json").write_text(json.dumps({"slug": "home-ledger", "name": "master@1"}), encoding="utf-8")
    out = run(recorded("claude_plan_accept", session_id="s1"), capsys, "claude", monkeypatch)
    assert "--slug home-ledger --as master@1 plan phases" in out


@pytest.mark.parametrize("content", ["{}", "not json", "[1]", None])
def test_an_unreadable_binding_names_placeholders(isolated, content, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    binding = isolated / ".sessions" / "s1.json"
    if content is None:
        binding.mkdir()
    else:
        binding.write_text(content, encoding="utf-8")
    out = decision.directive(recorded("claude_plan_accept", session_id="s1"))
    assert "`agentihooks ledger --slug <slug> --as <your name> plan phases <phases.json>`" in out


def test_an_unreadable_binding_falls_back_to_the_swarm(isolated, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "env-swarm")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@2")
    (isolated / ".sessions" / "s1.json").write_text("[1]", encoding="utf-8")
    out = decision.directive(recorded("claude_plan_accept", session_id="s1"))
    assert "--slug env-swarm --as master@2 plan phases" in out


def test_the_toolbelt_rule_bars_automatic_creation():
    rule = TOOLBELT.read_text(encoding="utf-8")
    section = rule.split("## Swarm, small ledger or nothing", 1)[1].split("\n## ", 1)[0]
    assert "No session creates a ledger or starts a swarm on its own" in section
    assert "gets no directive and" in section
    assert "keeps its ledger duties" in section
