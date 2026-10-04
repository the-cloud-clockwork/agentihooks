import re
from pathlib import Path

from scripts.swarm.cli import SETTABLE, build_parser

PAGE = Path(__file__).resolve().parents[2] / "docs" / "pillars" / "swarm.md"


def _text():
    return PAGE.read_text()


def test_page_has_front_matter_under_the_pillars():
    head = _text().split("---")[1]
    assert "parent: The Four Pillars" in head
    assert "permalink: /docs/pillars/swarm/" in head


def test_every_cli_command_is_documented():
    commands = next(a for a in build_parser()._actions if a.dest == "command").choices
    text = _text()
    for name in [*commands, "list", "tick"]:
        assert re.search(rf"agentihooks swarm (<id> )?{name}\b", text), name


def test_settable_caps_are_documented():
    for key in SETTABLE:
        assert key in _text()


def test_runtime_facts_are_documented():
    text = _text()
    for fact in ("AGENTIHOOKS_SWARM_REDIS_URL", "every minute", "one task", "eng", "ci", "<id>-master-<n>"):
        assert fact in text, fact
