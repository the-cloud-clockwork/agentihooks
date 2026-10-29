import re
from pathlib import Path

import pytest

from scripts.swarm import prompt

ROOT = Path(__file__).resolve().parents[2]
SHOW = re.compile(r"agentihooks ledger(?:\s+--(?:slug|as)\s+(?:<[^>]+>|[\w@.-]+))*\s+show\b")


@pytest.mark.parametrize("lane", ["master", "dispatch", "eng", "ci", "plan"])
def test_every_role_prompt_reads_items_instead_of_requesting_a_full_show(lane, monkeypatch):
    monkeypatch.setattr(prompt, "summary_lines", lambda slug: [])
    text = prompt.build("sw", "/repo", lane, "agent", {"id": "t1", "title": "Task", "phase": "p1"})
    assert "agentihooks ledger --slug sw get" in text
    assert "read the ledger in full" not in text.lower()
    for line in text.splitlines():
        if SHOW.search(line):
            assert "never run agentihooks ledger" in line.lower()


@pytest.mark.parametrize("role", ["engineer", "planner"])
def test_operator_opening_prompt_reads_an_item(role):
    text = prompt.build_operator("sw", "/repo", "agent", role, role)
    assert "agentihooks ledger --slug sw get" in text
    assert SHOW.search(text) is None


def test_packaged_role_and_skill_text_never_directs_a_full_ledger_show():
    paths = [*ROOT.glob("profiles/package/skills/**/*.md"), *ROOT.glob("profiles/package/roles/**/*.md")]
    assert paths
    for path in paths:
        for line in path.read_text().splitlines():
            if SHOW.search(line):
                assert "never" in line.lower(), f"{path.relative_to(ROOT)}: {line}"


@pytest.mark.parametrize(
    "command",
    [
        "agentihooks ledger show",
        "agentihooks ledger --slug sw show",
        "agentihooks ledger --slug <slug> --as <name> show",
    ],
)
def test_full_show_scanner_recognizes_commands(command):
    assert SHOW.search(command)


def test_full_show_scanner_does_not_match_show_in_later_prose():
    assert SHOW.search("agentihooks ledger --slug sw priority add <item> text. Approvals show in Priorities.") is None
