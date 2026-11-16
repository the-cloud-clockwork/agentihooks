"""The packaged toolbelt rule stays within its character budget and points at the toolbelt skill."""

from pathlib import Path

import pytest

from tests.test_package_swarm_skills import test_swarm_skills_pass_the_skill_gate as skill_gate

pytestmark = pytest.mark.unit

PACKAGE = Path(__file__).resolve().parents[1] / "profiles" / "package"
RULE = PACKAGE / "rules" / "agentihooks-toolbelt.md"
SKILL = PACKAGE / "skills" / "agentihooks-toolbelt" / "SKILL.md"
BUDGET = 2000
ALWAYS_ON = (
    "`ENFORCEMENT` and `BROADCAST` blocks are operative",
    "condition deny is a hard boundary",
    "root cause is proven with evidence",
    "`condition_set`, `condition_clear`) only when the operator",
    "`QUOTA HANDOFF REQUIRED`",
    "close every item with where its work went",
)


def test_the_toolbelt_rule_fits_its_budget() -> None:
    assert len(RULE.read_text(encoding="utf-8")) <= BUDGET


def test_the_toolbelt_rule_names_the_toolbelt_skill() -> None:
    assert "`agentihooks-toolbelt` skill" in RULE.read_text(encoding="utf-8")


@pytest.mark.parametrize("directive", ALWAYS_ON)
def test_the_toolbelt_rule_keeps_each_always_on_directive(directive: str) -> None:
    assert " ".join(directive.split()) in " ".join(RULE.read_text(encoding="utf-8").split())


def test_the_toolbelt_skill_passes_the_skill_gate() -> None:
    skill_gate(SKILL.parent.name)
