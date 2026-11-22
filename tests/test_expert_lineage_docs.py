"""The expert lineage reference page: every command it names is one the command line takes, the Stop gate is named by
a mode it has, and what the page describes before it is built is marked."""

import re
import shlex
from pathlib import Path

import pytest

from scripts.gates.expert_stop import ExpertStop
from scripts.gates.modes import MODES
from tests.profiles.test_expert_role import PARSERS

pytestmark = pytest.mark.unit

PAGE = Path(__file__).resolve().parents[1] / "docs" / "reference" / "expert-lineage.md"


def _named(text: str) -> list[str]:
    """Each agentihooks command the page names, in a code span or on a line of its own in a fenced block."""
    spans = re.findall(r"`(agentihooks [^`\n]+)`", text)
    return [*spans, *(line.strip() for line in text.splitlines() if line.startswith("agentihooks "))]


def _argv(command: str) -> tuple[str, list[str]]:
    """The tool and the arguments of a documented command, its optional parts and any redirect left out and its
    placeholders filled."""
    typed = command.replace("\\|", "|").partition(" > ")[0]
    plain = re.sub(r"<[^>]+>", "x", re.sub(r"\[[^\]]*\]", "", typed))
    tokens = [token.split("|")[0] for token in shlex.split(plain)[1:]]
    return tokens[0], tokens[1:]


def test_every_command_the_lineage_page_names_is_one_the_command_line_takes() -> None:
    named = _named(PAGE.read_text())
    assert len(named) > 8
    for command in named:
        tool, argv = _argv(command)
        assert PARSERS[tool](argv), command


def test_the_lineage_page_names_the_stop_gate_by_a_mode_it_has() -> None:
    text = PAGE.read_text()
    assert ExpertStop.default_mode in MODES
    assert f"Its default mode is `{ExpertStop.default_mode}`" in text
    assert f"keeps the Stop gate at `{ExpertStop.default_mode}`" in text
    assert [mode for mode in re.findall(r"expert-stop-gate=(\w+)", text) if mode not in MODES] == []
    assert "`deny`" not in text


def test_the_lineage_page_marks_what_it_describes_before_it_is_built() -> None:
    assert "## Not yet built" in PAGE.read_text()
