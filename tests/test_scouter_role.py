import json
import subprocess
import sys

import yaml

from tests.test_package_roles import GUARDS, PROMPTS, ROLES
from tests.test_profile_render import world as render_world

world = render_world
SCOUTER = ROLES / "scouter"
ALLOWED = [
    "Read(*)",
    "Glob(*)",
    "Grep(*)",
    "WebSearch",
    "WebFetch",
    "mcp__playwright-cmd",
    "Bash(agentihooks scout:*)",
    "Bash(agentihooks msg send:*)",
]
DENIED = ["Write", "Edit", "MultiEdit", "NotebookEdit"]


def test_scouter_renders_as_a_package_role_with_only_read_and_web_tools(world):
    from scripts.profiles import render

    world["install"]._save_state({})
    out = render.render_claude("scouter")

    assert (SCOUTER / "CLAUDE.md").read_text().strip() in (out / "CLAUDE.md").read_text()
    assert json.loads((out / render.STAMP).read_text())["chain"] == ["scouter"]
    permissions = json.loads((out / "settings.json").read_text())["permissions"]
    assert permissions == {"allow": ALLOWED, "deny": DENIED}
    assert "playwright-cmd" in json.loads((out / ".claude.json").read_text())["mcpServers"]


def test_scouter_defaults_to_high_tier_and_medium_effort():
    manifest = yaml.safe_load((SCOUTER / "profile.yml").read_text())

    assert (manifest["name"], manifest["tier"], manifest["effort"]) == ("scouter", "high", "medium")


def test_scouter_loads_the_planner_write_guards(world, monkeypatch):
    from hooks.context import conditions

    world["install"]._save_state({})
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", "scouter")
    entries, invalid = conditions.scan_layers(conditions.layer_dirs({})[0])

    assert sorted(e["file"] for e in entries if e["source"] == "profile:scouter") == sorted(GUARDS | {PROMPTS})
    assert invalid == []


def _guard(tmp_path, condition, tool, tool_input):
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(tmp_path / "repo")}
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "config"), "PATH": "/usr/bin:/bin"}
    script = SCOUTER / ".claude" / "conditions" / condition
    return subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env
    )


def test_scouter_conditions_deny_writes_and_pass_reads(tmp_path):
    edit = "pre-edit+write+notebookedit-no_code_edits.py"
    serena = "pre-mcp__serena-no_serena_writes.py"
    repo = tmp_path / "repo"

    write = _guard(tmp_path, edit, "Write", {"file_path": str(repo / "app.py")})
    notebook = _guard(tmp_path, edit, "NotebookEdit", {"notebook_path": str(repo / "n.ipynb")})
    scratch = _guard(tmp_path, edit, "Write", {"file_path": str(tmp_path / "scratchpad" / "evidence.json")})
    symbol = _guard(tmp_path, serena, "mcp__serena__replace_symbol_body", {"relative_path": "a.py"})
    read = _guard(tmp_path, serena, "mcp__serena__find_symbol", {"relative_path": "a.py"})

    assert [r.returncode for r in (write, notebook, scratch, symbol, read)] == [2, 2, 0, 2, 0]
    assert write.stderr == "scouter cannot edit outside ~/scratchpad; send the change to its author.\n"
