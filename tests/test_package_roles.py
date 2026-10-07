import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_profile_render import world as render_world

world = render_world
ROLES = Path(__file__).resolve().parents[1] / "profiles" / "package" / "roles"
ALL_ROLES = ("master", "engineer", "cicd", "planner", "qa")
GUARDED = ("master", "planner", "qa")
GUARDS = {"pre-edit+write+notebookedit-no_code_edits.py", "pre-mcp__serena-no_serena_writes.py"}
GATED = ("engineer", "cicd", "qa")
GATES = {
    "pre-agent+task+sendmessage-subagent_budget.py",
    "pre-any-quiet_claim.sh",
    "pre-bash.agentihooks+bash.bash+bash.sh+bash.zsh+bash.eval-identity_pin.py",
    "pre-bash.gh+bash.agentihooks-intent_gate.py",
    "pre-bash.gh-rerun_budget.py",
    "pre-edit+write+multiedit+notebookedit+mcp__serena+bash.git-build_gate.py",
    "pre-monitor+taskoutput+bashoutput+bash.gh+bash.sleep+bash.agentihooks-watch_budget.py",
    "stop-claim_stop.py",
    "stop-push_stop.py",
}
PROMPTS = "pre-bash.rm+bash.rmdir+bash.bash+bash.sh+bash.zsh+bash.eval-prompt_guard.py"


@pytest.mark.parametrize("role", ALL_ROLES)
def test_role_renders_with_its_role_file_and_no_bundle(world, role):
    from scripts.profiles import render

    world["install"]._save_state({})
    out = render.render_claude(role)

    persona = (out / "CLAUDE.md").read_text()
    assert (ROLES / role / "CLAUDE.md").read_text().strip() in persona
    rule = (ROLES / role / ".claude" / "rules" / f"{role}-role.md").read_text()
    assert f"<!-- rule: {role}-role.md (rule) -->\n{rule.strip()}" in persona
    assert json.loads((out / render.STAMP).read_text())["chain"] == [role]
    assert "mcpServers" not in json.loads((out / "settings.json").read_text())
    assert not (ROLES / role / ".claude" / ".mcp.json").exists()


@pytest.mark.parametrize("role", ALL_ROLES)
def test_guard_conditions_load_for_master_planner_and_qa_only(world, monkeypatch, role):
    from hooks.context import conditions

    world["install"]._save_state({})
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", role)
    layers, _ = conditions.layer_dirs({})
    entries, invalid = conditions.scan_layers(layers)

    loaded = [e["file"] for e in entries if e["source"] == f"profile:{role}"]
    expected = (GUARDS if role in GUARDED else set()) | (GATES if role in GATED else set()) | {PROMPTS}
    assert sorted(loaded) == sorted(expected)
    assert invalid == []


@pytest.mark.parametrize("role", ALL_ROLES)
def test_role_rule_commands_are_agentihooks_commands(role):
    rule = (ROLES / role / ".claude" / "rules" / f"{role}-role.md").read_text()
    spans = re.findall(r"`([^`]+)`", rule)

    assert spans
    assert all(span.startswith("agentihooks ") for span in spans), spans


def _guard(tmp_path, role, condition, tool, tool_input, cwd):
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)}
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "config"), "PATH": "/usr/bin:/bin"}
    script = ROLES / role / ".claude" / "conditions" / condition
    return subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env
    )


@pytest.mark.parametrize("role", GUARDED)
def test_no_code_edits_guard_allows_only_the_scratchpad(tmp_path, role):
    edit = "pre-edit+write+notebookedit-no_code_edits.py"
    repo = tmp_path / "repo"

    denied = _guard(tmp_path, role, edit, "Edit", {"file_path": str(repo / "app.py")}, repo)
    relative = _guard(tmp_path, role, edit, "Write", {"file_path": "app.py"}, repo)
    scratch = _guard(tmp_path, role, edit, "Write", {"file_path": str(tmp_path / "scratchpad" / "n.md")}, repo)

    assert (denied.returncode, relative.returncode, scratch.returncode) == (2, 2, 0)
    assert f"{role} cannot edit" in denied.stderr


@pytest.mark.parametrize(("role", "code"), [("master", 0), ("planner", 0), ("qa", 2)])
def test_plans_folder_is_open_to_master_and_planner_only(tmp_path, role, code):
    edit = "pre-edit+write+notebookedit-no_code_edits.py"
    plan = tmp_path / "config" / "plans" / "p.md"

    assert _guard(tmp_path, role, edit, "Write", {"file_path": str(plan)}, tmp_path).returncode == code


@pytest.mark.parametrize("role", GUARDED)
def test_no_serena_writes_guard_denies_writes_and_passes_reads(tmp_path, role):
    serena = "pre-mcp__serena-no_serena_writes.py"
    repo = tmp_path / "repo"

    write = _guard(tmp_path, role, serena, "mcp__serena__replace_symbol_body", {"relative_path": "a.py"}, repo)
    rename = _guard(tmp_path, role, serena, "mcp__serena__rename_symbol", {"relative_path": "a.py"}, repo)
    read = _guard(tmp_path, role, serena, "mcp__serena__find_symbol", {"relative_path": "a.py"}, repo)

    assert (write.returncode, rename.returncode, read.returncode) == (2, 2, 0)


def test_package_data_ships_the_guard_scripts():
    import tomllib

    data = tomllib.loads((ROLES.parents[2] / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]

    assert "**/*.py" in data["profiles"]
