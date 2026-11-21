"""The planner's spawn prompt and steering for a plan task that enhances a thin task, and the phase plan prompt golden."""

from pathlib import Path
from typing import Any

import pytest

from scripts.swarm import prompt
from scripts.swarm_ledger import ledger_workspace

GOLDEN = Path(__file__).with_name("phase_plan_prompt.golden")
ENHANCEMENT_GOLDEN = Path(__file__).with_name("enhancement_prompt.golden")
PLANNER = "planner@abcdef-0001"
PHASE_PLAN: dict[str, Any] = {
    "id": "plan-one",
    "title": "Slice phase",
    "description": "Slice the login phase into pull request sized tasks.",
    "phase": "p1",
    "lane": "plan",
    "kind": "plan",
    "workspace": "/home/worker/.agentihooks/swarm/demo/tasks/plan-one",
}
TARGET: dict[str, Any] = {
    "id": "t-login",
    "title": "Login page keeps the session",
    "description": "Fix login.",
    "phase": "p1",
    "lane": "eng",
    "kind": "code",
    "territory": ["scripts/auth", "tests/auth"],
}
RULES = ("description under 40 words", "no done when sentence", "no named seam")
ENHANCEMENT: dict[str, Any] = {
    "id": "enhance-t-login",
    "title": "Plan the login task",
    "description": f"The thin check failed task t-login on: {', '.join(RULES)}. Its description: Fix login.",
    "phase": "p1",
    "lane": "plan",
    "kind": "plan",
    "enhances": "t-login",
    "workspace": "/home/worker/.agentihooks/swarm/demo/tasks/enhance-t-login",
}
DOC: dict[str, Any] = {
    "overview": "Project intent",
    "phases": [{"id": "p1", "title": "Login phase", "description": "Login mission"}],
    "tasks": [TARGET, ENHANCEMENT, PHASE_PLAN],
}
HEAD = (
    "This plan task enhances task t-login, too thin or vague for an engineer to claim: the thin check failed it on "
    "the rules this task's description lists. steering.md holds its title, phase, territory and description."
)
STEPS = (
    "enhances task t-login",
    f"agentihooks api --slug demo --as {PLANNER} get tasks/t-login",
    "scouter sub agents",
    "Edit no code.",
    "<!-- slice: <anchor> -->",
    "What will be built, Code areas touched, What might break, Regression tests, Resources needed and Protection plan",
    "Summary, Seams, Tests and Proof",
    "Done when sentence",
    "publish-plan <plan file> --phase p1",
    "task set t-login plan_url=<plan link> plan_slice=<anchor> description=",
    "done --slice t-login",
)


def render() -> str:
    """Return the phase plan prompt the golden file holds; a deliberate prompt change rewrites the golden from it."""
    return prompt.build("demo", "/repo", "plan", PLANNER, PHASE_PLAN)


@pytest.mark.feature("planner-enhancement-prompt")
def test_phase_plan_prompt_matches_its_golden_byte_for_byte() -> None:
    assert render() == GOLDEN.read_text(encoding="utf-8")


@pytest.mark.feature("planner-enhancement-prompt")
def test_phase_plan_steering_carries_no_enhancement_block() -> None:
    text = ledger_workspace.steering(PHASE_PLAN, DOC)

    assert "Enhancement target" not in text
    assert "t-login" not in text


@pytest.mark.feature("planner-enhancement-prompt")
def test_enhancement_prompt_names_target_failed_rules_and_every_step() -> None:
    text = prompt.build("demo", "/repo", "plan", PLANNER, ENHANCEMENT)

    missing = [line for line in (*RULES, *STEPS) if line not in text]
    assert missing == []
    assert HEAD in text.splitlines()
    assert "1. Read steering.md: the target task's record and the project and mission intent." in text.splitlines()
    assert "Slice only phase" not in text
    assert "--plan-slice <slice name>" not in text


@pytest.mark.feature("planner-enhancement-prompt")
def test_enhancement_prompt_matches_its_golden_byte_for_byte() -> None:
    text = prompt.build("demo", "/repo", "plan", PLANNER, ENHANCEMENT)

    assert text == ENHANCEMENT_GOLDEN.read_text(encoding="utf-8")


@pytest.mark.feature("planner-enhancement-prompt")
def test_enhancement_steering_pastes_target_title_description_territory_and_phase() -> None:
    text = ledger_workspace.steering(ENHANCEMENT, DOC)

    block = [
        "# enhance-t-login: Plan the login task",
        "",
        ENHANCEMENT["description"],
        "",
        "Enhancement target",
        "- Task: t-login: Login page keeps the session",
        "- Phase: p1",
        "- Territory: scripts/auth, tests/auth",
        "- Description: Fix login.",
        "",
        "Plan evidence",
    ]
    assert "\n".join(block) in text
    assert text.index("Enhancement target") < text.index("Project intent")


@pytest.mark.feature("planner-enhancement-prompt")
def test_enhancement_steering_names_a_target_missing_from_the_ledger() -> None:
    text = ledger_workspace.steering(ENHANCEMENT, {**DOC, "tasks": [ENHANCEMENT, PHASE_PLAN]})

    block = ["Enhancement target", "- Task: t-login is not in the ledger: block this task.", "", "Plan evidence"]
    assert "\n".join(block) in text


@pytest.mark.feature("planner-enhancement-prompt")
def test_enhancement_steering_leaves_unset_target_fields_blank() -> None:
    bare = {"id": "t-login"}
    text = ledger_workspace.steering(ENHANCEMENT, {**DOC, "tasks": [bare, ENHANCEMENT]})

    block = ["Enhancement target", "- Task: t-login: ", "- Phase: ", "- Territory: none", "- Description: ", ""]
    assert "\n".join(block) in text
