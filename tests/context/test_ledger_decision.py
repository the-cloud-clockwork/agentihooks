import json
import sqlite3
from pathlib import Path

import pytest

import hooks.context.ledger_decision as decision
from hooks import hook_manager
from hooks.targets.emitter import flush
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository
from tests.swarm_ledger import legacy_page

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


def task_create(n, session="s1"):
    return {
        "session_id": session,
        "hook_event_name": "PostToolUse",
        "tool_name": "TaskCreate",
        "tool_input": {"subject": f"task {n}", "description": "work"},
        "tool_response": {"task": {"id": str(n)}},
        "cwd": "/",
    }


def bash(command, stdout, session="s1"):
    return {
        "session_id": session,
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False},
        "cwd": "/",
    }


# Seam one: an accepted plan starts a swarm.


def test_claude_leaving_plan_mode_directs_init_swarm(capsys, monkeypatch):
    out = run(recorded("claude_plan_accept"), capsys, "claude", monkeypatch)
    assert MARK in out
    assert "init-swarm" in out
    assert "without asking" in out


def test_codex_accepting_its_plan_directs_init_swarm(capsys, monkeypatch):
    out = run(recorded("codex_plan_accept"), capsys, "codex", monkeypatch)
    assert MARK in out
    assert "init-swarm" in out


def test_codex_asking_for_a_plan_is_not_accepting_one(capsys, monkeypatch):
    assert MARK not in run(recorded("codex_plan_prompt"), capsys, "codex", monkeypatch)


def test_copilot_leaving_plan_mode_directs_init_swarm(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "copilot")
    payload = {
        "session_id": "c1",
        "hook_event_name": "PostToolUse",
        "tool_name": "exit_plan_mode",
        "tool_input": {"summary": "Write, run and delete hello.py"},
    }
    assert "init-swarm" in decision.directive(payload)


def test_the_acceptance_signal_follows_the_harness(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    assert decision.directive(recorded("codex_plan_accept", session_id="a")) == ""
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    assert decision.directive(recorded("claude_plan_accept", session_id="b")) == ""


# Seam two: non trivial work without a plan gets a small ledger.


def test_a_task_list_of_four_directs_a_small_ledger(capsys, monkeypatch):
    assert MARK not in run(todos(3), capsys, "claude", monkeypatch)
    out = run(todos(4), capsys, "claude", monkeypatch)
    assert MARK in out
    assert "small ledger" in out


def test_the_fourth_created_task_directs_a_small_ledger(capsys, monkeypatch):
    for n in range(3):
        assert MARK not in run(task_create(n), capsys, "claude", monkeypatch)
    assert "small ledger" in run(task_create(3), capsys, "claude", monkeypatch)


@pytest.mark.parametrize(
    "text",
    [
        "troubleshoot why the ledger page is blank",
        "Debug the failing shard",
        "investigate the slow Redis test",
        "refactor the inbox module",
        "we have a bug, start debugging it",
    ],
)
def test_a_troubleshooting_prompt_directs_a_small_ledger(text, capsys, monkeypatch):
    out = run(prompt(text), capsys, "claude", monkeypatch)
    assert MARK in out
    assert "small ledger" in out


def test_a_codex_troubleshooting_prompt_directs_a_small_ledger(capsys, monkeypatch):
    payload = recorded("codex_trivial", prompt="Investigate why the hello world script prints twice")
    assert "small ledger" in run(payload, capsys, "codex", monkeypatch)


@pytest.mark.parametrize("name", ["codex_trivial"])
def test_a_trivial_recorded_prompt_gets_nothing(name, capsys, monkeypatch):
    assert MARK not in run(recorded(name), capsys, "codex", monkeypatch)


def test_a_trivial_prompt_gets_nothing(capsys, monkeypatch):
    assert MARK not in run(prompt("rename the helper to build_block"), capsys, "claude", monkeypatch)


def test_the_small_ledger_directive_leaves_no_size_judgement(capsys, monkeypatch):
    out = run(prompt("troubleshoot why sum.py prints 5"), capsys, "claude", monkeypatch)
    assert "before any other tool call" in out
    assert "even for a one line fix" in out
    assert "--size small" in out and "--slug" not in out


# Seam three: once per trigger, never in a swarm or a bound session, silenced by a decline.


def test_each_directive_fires_once_per_trigger(capsys, monkeypatch):
    assert MARK in run(prompt("debug the gate"), capsys, "claude", monkeypatch)
    assert MARK not in run(prompt("now investigate the server"), capsys, "claude", monkeypatch)
    assert MARK in run(todos(5), capsys, "claude", monkeypatch)
    assert MARK not in run(todos(6), capsys, "claude", monkeypatch)
    assert MARK in run(recorded("claude_plan_accept", session_id="s1"), capsys, "claude", monkeypatch)
    assert MARK not in run(recorded("claude_plan_accept", session_id="s1"), capsys, "claude", monkeypatch)


def test_another_session_gets_its_own_directive(capsys, monkeypatch):
    assert MARK in run(prompt("debug the gate", "s1"), capsys, "claude", monkeypatch)
    assert MARK in run(prompt("debug the gate", "s2"), capsys, "claude", monkeypatch)


def test_an_accepted_plan_is_not_followed_by_a_small_ledger(capsys, monkeypatch):
    assert "init-swarm" in run(recorded("claude_plan_accept", session_id="s1"), capsys, "claude", monkeypatch)
    assert MARK not in run(todos(4), capsys, "claude", monkeypatch)


def test_a_swarm_agent_gets_no_small_ledger(capsys, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "swarm-buildout")
    assert MARK not in run(prompt("debug the gate"), capsys, "claude", monkeypatch)
    assert MARK not in run(todos(5), capsys, "claude", monkeypatch)


def test_a_swarm_agent_appends_its_plan_to_the_swarm_ledger(capsys, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "swarm-buildout")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@323133-1")
    out = run(recorded("codex_plan_accept"), capsys, "codex", monkeypatch)
    assert MARK in out
    assert "agentihooks ledger --slug swarm-buildout --as master@323133-1 plan phases" in out
    assert "init-swarm" not in out


def test_a_session_bound_to_a_ledger_gets_no_small_ledger(isolated, capsys, monkeypatch):
    (isolated / ".sessions" / "s1.json").write_text(json.dumps({"slug": "x", "name": "y"}), encoding="utf-8")
    assert MARK not in run(prompt("troubleshoot the page"), capsys, "claude", monkeypatch)
    assert MARK not in run(todos(5), capsys, "claude", monkeypatch)


def test_a_bound_master_appends_its_plan_to_its_own_ledger(isolated, capsys, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "other-swarm")
    session = RECORDED["claude_plan_accept"]["session_id"]
    binding = {"slug": "rig-grade-swarm", "name": "master@323133-0001", "role": "master"}
    (isolated / ".sessions" / f"{session}.json").write_text(json.dumps(binding), encoding="utf-8")
    out = run(recorded("claude_plan_accept"), capsys, "claude", monkeypatch)
    assert MARK in out
    assert "agentihooks ledger --slug rig-grade-swarm --as master@323133-0001 plan phases <phases.json>" in out
    assert "init-swarm" not in out
    assert "without asking" in out
    assert MARK not in run(recorded("claude_plan_accept"), capsys, "claude", monkeypatch)


def test_an_unbound_plan_still_directs_init_swarm(isolated, capsys, monkeypatch):
    (isolated / ".sessions" / "other.json").write_text(json.dumps({"slug": "x", "name": "y"}), encoding="utf-8")
    out = run(recorded("claude_plan_accept"), capsys, "claude", monkeypatch)
    assert "init-swarm" in out
    assert "plan phases" not in out


def ledger(isolated, slug):
    legacy_page.store(isolated, slug, {})


def bin_registry(isolated, entries):
    repo = SQLiteLedgerRepository(isolated / DATABASE)
    with repo.connect() as connection, connection:
        repo.save_registry(connection, "bin", entries)


def plan_accept(text, plan_file=None):
    payload = recorded("claude_plan_accept")
    payload["tool_input"]["plan"] = text
    if plan_file is not None:
        payload["tool_input"]["planFilePath"] = str(plan_file)
    return payload


def test_an_unbound_plan_naming_an_existing_ledger_is_offered_it_first(isolated, capsys, monkeypatch):
    ledger(isolated, "rig-grade-swarm")
    ledger(isolated, "other-ledger")
    out = run(plan_accept("# Plan\n\nContinue rig-grade-swarm, then other-ledger.\n"), capsys, "claude", monkeypatch)
    assert MARK in out
    assert "names the existing ledger rig-grade-swarm" in out
    assert "ask the operator whether the plan continues rig-grade-swarm" in out
    offer = out.index("agentihooks ledger --slug rig-grade-swarm --as <your name> plan phases <phases.json>")
    assert offer < out.index("init-swarm")
    assert "other-ledger" not in out
    assert MARK not in run(plan_accept("Continue rig-grade-swarm."), capsys, "claude", monkeypatch)


def test_the_offer_names_the_agent_and_reads_the_plan_file(isolated, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@1")
    ledger(isolated, "swarm-buildout")
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("Add two phases to Swarm-Buildout.\n", encoding="utf-8")
    out = decision.directive(plan_accept("no slug here", plan_file))
    assert "`agentihooks ledger --slug swarm-buildout --as engineer@1 plan phases <phases.json>`" in out


def test_a_copilot_summary_naming_a_ledger_is_offered_it(isolated, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "copilot")
    ledger(isolated, "rig-grade-swarm")
    payload = {
        "session_id": "c1",
        "hook_event_name": "PostToolUse",
        "tool_name": "exit_plan_mode",
        "tool_input": {"summary": "Two more phases for rig-grade-swarm"},
    }
    assert "names the existing ledger rig-grade-swarm" in decision.directive(payload)


@pytest.mark.parametrize(
    "text",
    [
        "# Hello world\n\n1. Write hello.py\n",
        "Start a fresh ledger called brand-new-ledger.",
        "Build the demo-app on top of the old demo-ledger.",
        "Revive the binned ledger.",
    ],
)
def test_a_plan_naming_no_existing_ledger_gets_the_swarm_directive(isolated, text, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    ledger(isolated, "demo")
    ledger(isolated, "binned")
    bin_registry(isolated, {"binned": 1})
    assert decision.directive(plan_accept(text, isolated / "missing.md")) == decision.SWARM_DIRECTIVE


def test_a_plan_without_text_names_no_ledger(isolated, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "copilot")
    for slug in ("none", "xxxx"):
        ledger(isolated, slug)
    payload = {"session_id": "c1", "hook_event_name": "PostToolUse", "tool_name": "exit_plan_mode", "tool_input": {}}
    assert decision.directive(payload) == decision.SWARM_DIRECTIVE


def test_an_unreadable_bin_hides_no_ledger(isolated, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    ledger(isolated, "binned")
    with sqlite3.connect(isolated / DATABASE) as connection:
        connection.execute("DROP TABLE registry")
    assert "names the existing ledger binned" in decision.directive(plan_accept("Revive the binned ledger."))


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


def test_a_recorded_decline_silences_the_session(capsys, monkeypatch):
    run(bash("agentihooks ledger decline", '{"declined": true}'), capsys, "claude", monkeypatch)
    assert MARK not in run(prompt("troubleshoot the page"), capsys, "claude", monkeypatch)
    assert MARK not in run(recorded("claude_plan_accept", session_id="s1"), capsys, "claude", monkeypatch)
    assert MARK in run(prompt("troubleshoot the page", "s2"), capsys, "claude", monkeypatch)


def test_a_failed_decline_does_not_silence(capsys, monkeypatch):
    run(bash("agentihooks ledger decline", "usage: agentihooks ledger"), capsys, "claude", monkeypatch)
    assert MARK in run(prompt("troubleshoot the page"), capsys, "claude", monkeypatch)


def test_the_decline_command_reports_the_decline(capsys):
    from scripts.swarm_ledger import run as ledger_run

    assert ledger_run(["decline"]) == 0
    assert json.loads(capsys.readouterr().out) == {"declined": True}


# Seam four: the toolbelt rule states the three outcomes.


def test_the_toolbelt_rule_states_the_three_outcomes():
    rule = TOOLBELT.read_text(encoding="utf-8")
    section = rule.split("## Swarm, small ledger or nothing", 1)[1].split("\n## ", 1)[0]
    assert "init-swarm" in section
    assert "small ledger" in section
    assert "agentihooks ledger decline" in section
    assert "trivial" in section
