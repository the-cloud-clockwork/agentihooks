import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILL = Path(__file__).parent.parent / "profiles/package/skills/init-swarm/SKILL.md"


def _help(*argv: str) -> str:
    done = subprocess.run(["agentihooks", *argv, "--help"], capture_output=True, text=True, check=True)
    return done.stdout


def test_skill_has_frontmatter():
    head = SKILL.read_text().split("---")[1]
    assert re.search(r"^name: init-swarm$", head, re.M)
    assert re.search(r"^description:", head, re.M)


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
