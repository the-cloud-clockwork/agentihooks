import json
import re
import shlex
from pathlib import Path

import pytest
import yaml

from scripts.quick import cli

pytestmark = pytest.mark.unit

SKILLS = Path(__file__).parents[2] / "profiles/package/skills"
TRIGGERS = {"quick-swarm-init": "start a quick swarm", "quick-swarm-handoff": "hand off this quick seat"}


@pytest.mark.feature("quick-swarm")
@pytest.mark.parametrize("name", sorted(TRIGGERS))
def test_each_quick_skill_passes_the_skill_gate(name):
    skill = SKILLS / name / "SKILL.md"
    _, front, body = skill.read_text().split("---", 2)
    meta = yaml.safe_load(front)
    assert meta["name"] == skill.parent.name == name
    assert 0 < len(meta["description"]) <= 1024 and not re.search("[<>]", meta["description"])
    assert TRIGGERS[name] in meta["description"]
    assert len(body.splitlines()) < 500
    evals = json.loads((skill.parent / "evals" / "evals.json").read_text())
    assert len(evals) >= 3 and all(e["query"] and e["expected_behavior"] for e in evals)


@pytest.mark.parametrize("name", sorted(TRIGGERS))
def test_every_quick_command_in_a_skill_parses(name):
    text = (SKILLS / name / "SKILL.md").read_text()
    commands = re.findall(r"`agentihooks quick ([^`]+)`", text)
    assert commands
    for command in commands:
        argv = [word if not word.startswith("<") else "x" for word in shlex.split(command)]
        assert cli.parser().parse_args(argv).command == argv[0]
