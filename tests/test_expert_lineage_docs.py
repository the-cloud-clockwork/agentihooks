"""The expert lineage reference page: every command it names is one the command line takes, the Stop gate is named by
the mode names the command line prints, and what the page describes before it is built is marked."""

import re
import shlex
from pathlib import Path

import pytest

from scripts.gates import modes
from scripts.gates.expert_stop import ExpertStop
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


def test_the_lineage_page_names_the_stop_gate_by_the_mode_names_the_command_line_prints() -> None:
    text = PAGE.read_text()
    default = modes.label(ExpertStop.default_mode)
    *first, last = (f"`{modes.label(mode)}`" for mode in modes.MODES)
    assert f"Its default mode is `{default}`" in text
    assert f"keeps the Stop gate at `{default}`" in text
    assert f"where the mode is {', '.join(first)} or {last}" in text


def test_the_lineage_page_marks_what_it_describes_before_it_is_built() -> None:
    assert "## Not yet built" in PAGE.read_text()
