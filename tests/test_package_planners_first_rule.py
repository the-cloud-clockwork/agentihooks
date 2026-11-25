"""The planners first rule, its pointers in the plan publishing roles and skills, and the planner skill's one task plan."""

from pathlib import Path

import pytest

from tests.test_profile_render import _flat

PACKAGE = Path(__file__).resolve().parents[1] / "profiles" / "package"
ROLES = PACKAGE / "roles"
RULE = PACKAGE / "rules" / "planners-first.md"
PLANNER_SKILL = ROLES / "planner" / ".claude" / "skills" / "swarm-planner" / "SKILL.md"
GUIDANCE = (
    ROLES / "master" / "CLAUDE.md",
    ROLES / "master" / ".claude" / "skills" / "swarm-master" / "SKILL.md",
    ROLES / "planner" / "CLAUDE.md",
    PLANNER_SKILL,
    PACKAGE / "skills" / "init-swarm" / "SKILL.md",
    PACKAGE / "skills" / "take-master" / "SKILL.md",
)
GUIDANCE_IDS = ("master", "swarm-master", "planner", "swarm-planner", "init-swarm", "take-master")
STANDALONE = (
    "Follow ups, open questions, operator notes and orders the operator types or gives are standalone tasks with "
    "no plan and no slice."
)
POINTER = "planners-first rule"
ROUTING = (
    "small in context, vague, or wider than one pull request",
    "before any engineer or ci agent claims it",
    '`agentihooks api --slug <slug> --as <name> phase add <id> "<title>" --planning auto`',
    "`max-plan-agents` above zero while plan work is open",
    "`agentihooks swarm <slug> focus tasks/plan-<id>`",
    "never hand write the spec",
    "publishes it with slice markers",
    "every task built from it carries its slice",
    "Only a small self explanatory change stays a standalone task",
    "`agentihooks swarm <slug> set max-eng-agents=0 max-ci-agents=0 max-plan-agents=0`",
)
PLAN_PARTS = (
    "what will be built",
    "code areas touched",
    "what might break",
    "regression tests and feature map entries",
    "resources needed",
    "protection plan",
    "done condition",
)


@pytest.mark.feature("planners-first-rule")
def test_rule_sends_thin_vague_or_wide_work_to_a_planner_first() -> None:
    raw = RULE.read_text()
    head = raw.split("---")[1]
    text = _flat(raw)

    assert "priority: " in head and "description: " in head
    assert [line for line in ROUTING if line not in text] == []
    places = [text.index(part) for part in PLAN_PARTS]
    assert places == sorted(places)


@pytest.mark.feature("planners-first-rule")
@pytest.mark.parametrize("guidance", GUIDANCE, ids=GUIDANCE_IDS)
def test_plan_publishers_point_at_the_rule_instead_of_the_standalone_line(guidance: Path) -> None:
    text = _flat(guidance.read_text())

    assert POINTER in text
    assert STANDALONE not in text


@pytest.mark.feature("planners-first-rule")
def test_no_package_file_keeps_the_standalone_line() -> None:
    held = [
        str(path.relative_to(PACKAGE))
        for path in sorted(PACKAGE.rglob("*.md"))
        if STANDALONE in _flat(path.read_text())
    ]

    assert held == []


@pytest.mark.feature("planners-first-rule")
def test_planner_skill_plans_one_thin_task_as_well_as_a_phase() -> None:
    raw = PLANNER_SKILL.read_text()
    description = _flat(raw.split("---")[1])
    slice_section = _flat(raw.split("\n## Slice\n", 1)[1].split("\n## ", 1)[0])

    assert "one thin or vague task" in description
    assert "names a single thin or vague task" in slice_section
    assert [part for part in PLAN_PARTS if part not in slice_section] == []
