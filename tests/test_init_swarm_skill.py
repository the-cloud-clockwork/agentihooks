import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

SKILL = Path(__file__).parent.parent / "profiles/package/skills/init-swarm/SKILL.md"


def _help(*argv: str) -> str:
    done = subprocess.run(["agentihooks", *argv, "--help"], capture_output=True, text=True, check=True)
    return done.stdout


def test_skill_has_frontmatter():
    head = SKILL.read_text().split("---")[1]
    assert re.search(r"^name: init-swarm$", head, re.M)
    assert re.search(r"^description:", head, re.M)


def test_skill_passes_the_skill_gate():
    _, front, body = SKILL.read_text().split("---", 2)
    meta = yaml.safe_load(front)
    assert re.match(r"^[a-z0-9-]{1,64}$", meta["name"]) and meta["name"] == SKILL.parent.name
    assert 0 < len(meta["description"]) <= 1024 and not re.search("[<>]", meta["description"])
    assert len(body.splitlines()) < 500
    evals = json.loads((SKILL.parent / "evals" / "evals.json").read_text())
    assert len(evals) >= 3 and all(e["query"] and e["expected_behavior"] for e in evals)


def test_every_command_exists():
    text = SKILL.read_text()
    swarm_help = _help("swarm")
    ledger_help = _help("ledger")
    swarm = set(re.findall(r"agentihooks swarm <slug> (\w[\w-]*)", text))
    ledger = set(re.findall(r"agentihooks ledger(?: --slug <slug>)? (\w[\w-]*)", text))
    assert {"create", "start"} <= swarm
    assert {"new", "task"} <= ledger
    assert all(re.search(rf"\b{c}\b", swarm_help) for c in swarm)
    assert all(re.search(rf"\b{c}\b", ledger_help) for c in ledger - {"new"})
