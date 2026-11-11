"""The tick-step skill, its evals and the tick pattern package rule every role persona folds in."""

import json
import re
from pathlib import Path

import pytest
import yaml

from tests.test_profile_render import world as render_world

world = render_world
PACKAGE = Path(__file__).resolve().parents[1] / "profiles" / "package"
SKILL = PACKAGE / "skills" / "tick-step" / "SKILL.md"
EVALS = SKILL.parent / "evals"
RULE = PACKAGE / "rules" / "tick-pattern.md"
ENGINEER = PACKAGE / "roles" / "engineer" / ".claude" / "skills" / "swarm-engineer" / "SKILL.md"
MASTER = PACKAGE / "roles" / "master" / ".claude" / "skills" / "swarm-master" / "SKILL.md"
ROLES = ("master", "engineer", "cicd", "planner", "qa")
COMMANDS = (
    "python -m scripts.tick.new_step",
    'python -m scripts.tick_pattern --base "$BASE"',
    "BASE=$(wt.sh tmp --from origin/dev",
    "python -m scripts.tick.catalogue check",
    "python -m scripts.feature_map",
)
ARTEFACTS = (
    "scripts/tick/steps/",
    "scripts/tick/steps/budgets.py",
    "tests/tick/steps/test_",
    "tests/swarm/tick_scenarios/steps.json",
    "feature-map.json",
)


def _split(path: Path) -> tuple[dict, str]:
    """The YAML frontmatter and the body of the Markdown file at ``path``."""
    _, front, body = path.read_text().split("---", 2)
    return yaml.safe_load(front), body


def _flat(text: str) -> str:
    """``text`` with every run of whitespace collapsed to one space."""
    return " ".join(text.split())


def test_tick_step_skill_frontmatter_meets_the_authoring_limits() -> None:
    meta, body = _split(SKILL)

    assert meta["name"] == "tick-step"
    assert 0 < len(meta["description"]) <= 1024
    assert not re.search("[<>]", meta["description"])
    assert "Use when" in meta["description"]
    assert len(body.splitlines()) < 500
    assert not re.search(r"manifesto|§|~/\.claude/skills", SKILL.read_text(), re.IGNORECASE)


@pytest.mark.parametrize("command", COMMANDS)
def test_tick_step_skill_names_each_command(command: str) -> None:
    assert command in _flat(SKILL.read_text())


def test_tick_step_skill_asks_for_the_run_ids_in_the_pull_request() -> None:
    body = _flat(SKILL.read_text()).lower()

    assert "equivalence" in body
    assert "planted fault" in body
    assert "run id" in body


def test_tick_step_evals_cover_three_queries() -> None:
    evals = json.loads((EVALS / "evals.json").read_text())

    assert len(evals) >= 3
    assert all(entry["query"] and entry["expected_behavior"] for entry in evals)


@pytest.mark.parametrize("artefact", ARTEFACTS)
def test_tick_step_eval_transcript_shows_the_five_artefacts(artefact: str) -> None:
    transcript = (EVALS / "transcript.md").read_text()

    assert "python -m scripts.tick.new_step" in transcript
    assert artefact in transcript


def test_tick_pattern_rule_carries_package_rule_frontmatter() -> None:
    meta, body = _split(RULE)

    assert meta["description"]
    assert isinstance(meta["priority"], int)
    assert len([line for line in body.splitlines() if line.strip()]) <= 14
    assert "python -m scripts.tick.new_step" in body
    assert "python -m scripts.tick_pattern" in body
    assert "tick-step" in body
    points = re.findall(r"^(\d)\. (.+)$", body, re.MULTILINE)
    assert [number for number, _ in points] == ["1", "2", "3", "4", "5"]
    assert all(word in points[0][1] for word in ("group", "lane", "effects", "feature", "budgets.FLOORS"))
    assert "effects" in points[1][1] and "scripts/swarm/tick.py" in points[1][1]
    assert "steps.json" in points[2][1] and "feature-map.json" in points[2][1]
    assert "python -m scripts.tick.catalogue check" in points[3][1]
    assert "equivalence" in points[4][1] and "planted fault" in points[4][1]
    assert not re.search(r"manifesto|§|~/\.claude/skills", RULE.read_text(), re.IGNORECASE)


def test_role_list_covers_every_package_role_folder() -> None:
    folders = {path.parent.name for path in (PACKAGE / "roles").glob("*/profile.yml")}

    assert {"dispatcher", "judge", "documentation"} <= folders
    assert sorted(ROLES) == sorted(folders)


@pytest.mark.parametrize("role", ROLES)
def test_tick_pattern_rule_folds_into_every_role_persona(world: dict, role: str) -> None:
    from scripts.profiles import render

    world["install"]._save_state({})
    out = render.render_claude(role)
    assert out is not None
    persona = (out / "CLAUDE.md").read_text()

    assert persona.count(f"<!-- rule: tick-pattern.md (rule) -->\n{RULE.read_text().strip()}") == 1


def test_swarm_engineer_loads_tick_step_first_for_a_tick_task() -> None:
    work = ENGINEER.read_text().split("## Work", 1)[1].split("\n## ", 1)[0]

    assert "tick-step" in work


@pytest.mark.parametrize("field", ["names the step", "its group", "lane (", "effects", "feature map id"])
def test_swarm_master_tick_task_spec_names_each_field(field: str) -> None:
    tasks = MASTER.read_text().split("## Tasks and proof contracts", 1)[1].split("\n## ", 1)[0]
    bullet = next(item for item in tasks.split("\n- ") if "tick task" in item.lower())

    assert field in _flat(bullet)
