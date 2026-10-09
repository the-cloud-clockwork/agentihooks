import json
import re
from pathlib import Path

import pytest
import yaml

from scripts.swarm.cli import build_parser

pytestmark = pytest.mark.unit

SKILL = Path(__file__).parent.parent / "profiles/package/skills/take-master/SKILL.md"


def test_skill_passes_the_skill_gate():
    _, front, body = SKILL.read_text().split("---", 2)
    meta = yaml.safe_load(front)
    assert meta["name"] == SKILL.parent.name == "take-master"
    assert 0 < len(meta["description"]) <= 1024 and not re.search("[<>]", meta["description"])
    assert "you are the master of ledger" in meta["description"]
    assert len(body.splitlines()) < 500
    evals = json.loads((SKILL.parent / "evals" / "evals.json").read_text())
    assert len(evals) >= 3 and all(e["query"] and e["expected_behavior"] for e in evals)


def test_the_skill_runs_a_command_the_parser_accepts():
    for flags in re.findall(r"agentihooks swarm <slug> take-master((?: --[a-z]+)*)", SKILL.read_text()):
        assert build_parser().parse_args(["s", "take-master", *flags.split()]).command == "take-master"


def test_a_master_plan_carries_slice_anchors_and_every_task_its_slice():
    text = SKILL.read_text()

    assert "<!-- slice: <id> -->" in text
    assert "agentihooks ledger --slug <slug> --as <name> publish-plan <plan file> --phase <phase ids>" in text
    assert "--plan-slice <id>" in text
