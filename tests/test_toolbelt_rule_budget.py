"""The packaged toolbelt rule stays within its character budget and points at the toolbelt skill."""

from pathlib import Path

import pytest

from tests.test_package_swarm_skills import test_swarm_skills_pass_the_skill_gate as skill_gate

pytestmark = pytest.mark.unit

PACKAGE = Path(__file__).resolve().parents[1] / "profiles" / "package"
RULE = PACKAGE / "rules" / "agentihooks-toolbelt.md"
SKILL = PACKAGE / "skills" / "agentihooks-toolbelt" / "SKILL.md"
BUDGET = 2000


def test_the_toolbelt_rule_fits_its_budget() -> None:
    assert len(RULE.read_text(encoding="utf-8")) <= BUDGET


def test_the_toolbelt_rule_names_the_toolbelt_skill() -> None:
    assert "`agentihooks-toolbelt` skill" in RULE.read_text(encoding="utf-8")


def test_the_toolbelt_skill_passes_the_skill_gate() -> None:
    skill_gate(SKILL.parent.name)
