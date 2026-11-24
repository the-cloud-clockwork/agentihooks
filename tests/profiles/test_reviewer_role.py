"""The reviewer package role: its Claude and Codex renders, its edit refusal and its strict review rule."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from tests.test_profile_render import world as world

ROLE = Path(__file__).resolve().parents[2] / "profiles" / "package" / "roles" / "reviewer"
SKILL = ROLE / ".claude" / "skills" / "swarm-reviewer"
STRICT_RULE = (
    "Review independently of check results: red, green or pending, you judge the change itself.",
    "Read the change request body, the whole diff, the feature map and the code the change reaches, and look for "
    "what the delivery breaks.",
    "Never close a review with zero findings.",
    "You never edit or code.",
)


def _condition(home: Path, name: str, tool: str, tool_input: dict[str, str]) -> subprocess.CompletedProcess[str]:
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(home / "repo")}
    env = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / "config"), "PATH": "/usr/bin:/bin"}
    script = ROLE / ".claude" / "conditions" / name
    return subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env
    )


@pytest.mark.feature("reviewer-role-renders-read-only-for-claude-and-codex")
def test_reviewer_renders_for_claude_and_codex_with_its_skill(world: dict[str, Any]) -> None:
    from scripts.profiles import render

    world["install"]._save_state({})
    claude = render.render_claude("reviewer")
    codex = render.render_codex("reviewer")
    assert claude is not None and codex is not None

    assert (ROLE / "CLAUDE.md").read_text().strip() in (claude / "CLAUDE.md").read_text()
    assert (claude / "skills" / "swarm-reviewer").resolve() == SKILL
    assert (codex / "skills" / "swarm-reviewer" / "SKILL.md").resolve() == SKILL / "SKILL.md"
    posture = tomllib.loads((codex / "config.toml").read_text())
    assert (posture["approval_policy"], posture["sandbox_mode"]) == ("never", "danger-full-access")
    assert render.stamp("reviewer")["chain"] == ["reviewer"]


@pytest.mark.feature("reviewer-role-renders-read-only-for-claude-and-codex")
def test_a_reviewer_session_refuses_code_edits_and_serena_writes(tmp_path: Path) -> None:
    edit = _condition(
        tmp_path,
        "pre-edit+write+notebookedit-no_code_edits.py",
        "Edit",
        {"file_path": str(tmp_path / "repo" / "app.py")},
    )
    serena = _condition(
        tmp_path, "pre-mcp__serena-no_serena_writes.py", "mcp__serena__replace_symbol_body", {"relative_path": "a.py"}
    )

    assert (edit.returncode, serena.returncode) == (2, 2)
    assert "reviewer cannot edit outside ~/scratchpad; send the change to its author." in edit.stderr


@pytest.mark.feature("reviewer-role-renders-read-only-for-claude-and-codex")
def test_reviewer_persona_holds_the_strict_independent_review_rule() -> None:
    persona = " ".join((ROLE / "CLAUDE.md").read_text().split())

    assert [rule for rule in STRICT_RULE if rule not in persona] == []
