import json
import re
from pathlib import Path

import pytest
import yaml

from tests.test_profile_render import _write
from tests.test_profile_render import world as render_world

world = render_world
PACKAGE = Path(__file__).resolve().parents[1] / "profiles" / "package"
ROLES = ("master", "engineer", "cicd", "planner", "qa")
SWARM_SKILLS = ("handoff", "triage-priorities", "worktree")
RULES = ("code-intelligence.md", "worktrees.md")


def _listed(out: Path) -> list[str]:
    return sorted(p.name for p in (out / "skills").iterdir())


@pytest.mark.parametrize("role", ROLES)
def test_every_role_home_lists_the_swarm_skills_once_with_no_bundle(world, role):
    from scripts.profiles import render

    world["install"]._save_state({})
    out = render.render_claude(role)

    listed = _listed(out)
    assert [listed.count(name) for name in SWARM_SKILLS] == [1, 1, 1]
    for name in SWARM_SKILLS:
        assert (out / "skills" / name).resolve() == PACKAGE / "skills" / name


@pytest.mark.parametrize("role", ROLES)
def test_every_role_home_lists_the_swarm_skills_once_with_a_bundle_copy(world, role):
    from scripts.profiles import render

    for name in SWARM_SKILLS:
        _write(world["bundle"] / ".claude" / "skills" / name / "SKILL.md", f"---\nname: {name}\n---\n")
    for name in RULES:
        _write(world["bundle"] / ".claude" / "rules" / name, "BUNDLE COPY\n")
    out = render.render_claude(role)

    listed = _listed(out)
    assert [listed.count(name) for name in SWARM_SKILLS] == [1, 1, 1]
    persona = (out / "CLAUDE.md").read_text()
    assert [persona.count(f"<!-- rule: {name} (rule) -->") for name in RULES] == [1, 1]


@pytest.mark.parametrize("role", ROLES)
def test_package_rules_fold_into_every_role_persona(world, role):
    from scripts.profiles import render

    world["install"]._save_state({})
    persona = (render.render_claude(role) / "CLAUDE.md").read_text()

    for name in RULES:
        rule = (PACKAGE / "rules" / name).read_text().strip()
        assert persona.count(f"<!-- rule: {name} (rule) -->\n{rule}") == 1


@pytest.mark.parametrize("name", RULES)
def test_package_rules_carry_no_manifesto_text(name):
    text = (PACKAGE / "rules" / name).read_text()

    assert not re.search(r"manifesto|§", text, re.IGNORECASE)
    assert "~/.claude/skills" not in text


@pytest.mark.parametrize("name", SWARM_SKILLS)
def test_swarm_skills_pass_the_skill_gate(name):
    skill = PACKAGE / "skills" / name / "SKILL.md"
    _, front, body = skill.read_text().split("---", 2)
    meta = yaml.safe_load(front)

    assert meta["name"] == name
    assert 0 < len(meta["description"]) <= 1024 and not re.search("[<>]", meta["description"])
    assert len(body.splitlines()) < 500
    evals = json.loads((skill.parent / "evals" / "evals.json").read_text())
    assert len(evals) >= 3 and all(e["query"] and e["expected_behavior"] for e in evals)
    assert not re.search(r"manifesto|§|~/\.claude/skills", skill.read_text(), re.IGNORECASE)


def test_swarm_skill_scripts_are_executable():
    scripts = [p for name in SWARM_SKILLS for p in (PACKAGE / "skills" / name).rglob("scripts/*") if p.is_file()]

    assert {p.name for p in scripts} == {"wt.sh", "list_priorities.py", "apply_answers.py"}
    assert all(p.stat().st_mode & 0o111 for p in scripts)
