"""The packaged toolbelt rule stays within its character budget and points at the toolbelt skill."""

import json
import re
from pathlib import Path

import pytest
import yaml

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
    text = SKILL.read_text(encoding="utf-8")
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front)

    assert meta["name"] == "agentihooks-toolbelt"
    assert 0 < len(meta["description"]) <= 1024 and not re.search("[<>]", meta["description"])
    assert "Use when" in meta["description"]
    assert len(body.splitlines()) < 500
    evals = json.loads((SKILL.parent / "evals" / "evals.json").read_text(encoding="utf-8"))
    assert len(evals) >= 3 and all(e["query"] and e["expected_behavior"] for e in evals)
    assert not re.search(r"manifesto|§|~/\.claude/skills", text, re.IGNORECASE)
