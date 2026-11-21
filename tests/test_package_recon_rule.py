"""The recon before edit rule, its pointers in the role personas and skills, and the start of task behaviour they extend."""

from pathlib import Path
from typing import Any

import pytest

from tests.test_profile_render import _flat
from tests.test_profile_render import world as world

PACKAGE = Path(__file__).resolve().parents[1] / "profiles" / "package"
ROLES = PACKAGE / "roles"
RULE = PACKAGE / "rules" / "recon-before-edit.md"
RENDERED_ROLES = ("master", "engineer", "cicd", "planner", "qa")
HEADINGS = (
    "## What will be built",
    "## Code areas touched",
    "## What might break",
    "## Regression tests",
    "## Resources needed",
    "## Protection plan",
)
SCOUTER = (
    "Your first act on every task is launching the scouter sub agent to check, with evidence, that the task and "
    "the architecture it assumes are still valid against current `dev`. Valid: proceed. Outdated or not feasible: "
    'build nothing; send the scouter finding to the master with `agentihooks msg send master@<slug> "<finding>"` '
    'and block the task with `agentihooks swarm <slug> block "<evidence>"`.'
)
POINTER = "recon-before-edit rule"
SPEC_AREAS = "names the code areas and the working behaviours the task touches"


def _text(path: Path) -> str:
    return _flat(path.read_text())


def _skill(role: str, name: str) -> str:
    return _text(ROLES / role / ".claude" / "skills" / name / "SKILL.md")


@pytest.mark.feature("package-rules-fold-into-every-role-persona")
@pytest.mark.parametrize("role", RENDERED_ROLES)
def test_every_package_rule_folds_into_every_role_persona_once(world: dict[str, Any], role: str) -> None:
    from scripts.profiles import render

    world["install"]._save_state({})
    persona = (render.render_claude(role) / "CLAUDE.md").read_text()

    rules = sorted(path for path in (PACKAGE / "rules").glob("*.md") if path.name != "README.md")
    assert rules
    for rule in rules:
        assert persona.count(f"<!-- rule: {rule.name} (rule) -->\n{rule.read_text().strip()}") == 1


@pytest.mark.feature("engineer-and-ci-personas-keep-the-scouter-first-act")
@pytest.mark.parametrize("role", ["engineer", "cicd"])
def test_engineer_and_ci_personas_keep_the_whole_scouter_first_act(role: str) -> None:
    assert SCOUTER in _text(ROLES / role / "CLAUDE.md")


@pytest.mark.feature("swarm-engineer-skill-traces-a-plan-before-work")
def test_swarm_engineer_skill_traces_a_plan_before_its_work() -> None:
    text = _skill("engineer", "swarm-engineer")

    assert text.index("## Plan") < text.index("## Work")
    assert "Before your first edit, a planner sub-agent writes `plan.md`" in text
    assert "`agentihooks swarm <slug> trace-plan`" in text


@pytest.mark.feature("recon-before-edit-rule")
def test_recon_rule_names_the_recon_file_and_its_six_headings_in_order() -> None:
    raw = RULE.read_text()
    head = raw.split("---")[1]
    text = _flat(raw)

    assert "priority: " in head and "description: " in head
    assert "`recon.md`" in text and "500 lines" in text
    assert "scouter sub agents" in text and "plan mode" in text
    places = [text.index(heading) for heading in HEADINGS]
    assert places == sorted(places)


@pytest.mark.feature("recon-before-edit-rule")
@pytest.mark.parametrize("role", ["engineer", "cicd"])
def test_engineer_and_ci_personas_point_at_the_recon_after_the_scouter(role: str) -> None:
    text = _text(ROLES / role / "CLAUDE.md")

    assert text.index(SCOUTER) < text.index(POINTER)


@pytest.mark.feature("recon-before-edit-rule")
def test_documentation_persona_points_at_the_recon_rule() -> None:
    assert POINTER in _text(ROLES / "documentation" / "CLAUDE.md")


@pytest.mark.feature("recon-before-edit-rule")
def test_master_and_planner_hold_task_specs_and_merges_to_the_recon() -> None:
    master = _text(ROLES / "master" / ".claude" / "rules" / "master-role.md")
    planner = _text(ROLES / "planner" / ".claude" / "rules" / "planner-role.md")

    assert SPEC_AREAS in master and SPEC_AREAS in planner
    assert "never queue or merge a pull request whose task work folder has no `recon.md`" in master


@pytest.mark.feature("recon-before-edit-rule")
@pytest.mark.parametrize(
    ("role", "name", "after"), [("engineer", "swarm-engineer", "## Plan"), ("cicd", "swarm-ci", "## Work")]
)
def test_role_skills_run_the_recon_before_the_first_edit(role: str, name: str, after: str) -> None:
    text = _skill(role, name)

    assert text.index("## Recon") < text.index(after)
    assert POINTER in text and "`recon.md`" in text
