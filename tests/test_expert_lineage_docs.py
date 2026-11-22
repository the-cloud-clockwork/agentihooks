"""The expert lineage reference page: every command it names is one the command line takes, the Stop gate is named by
the mode names the command line prints, each typed as the shell takes it, and what the page describes before it is
built is marked."""

import re
import shlex
from pathlib import Path

import pytest

from scripts.gates import modes
from scripts.gates.expert_stop import ExpertStop
from scripts.swarm import cli
from tests import command_lines

pytestmark = pytest.mark.unit

PAGE = Path(__file__).resolve().parents[1] / "docs" / "reference" / "expert-lineage.md"
SETTING = "expert-stop-gate"


def test_every_command_the_lineage_page_names_is_one_the_command_line_takes() -> None:
    named = command_lines.named(PAGE.read_text())
    assert len(named) > 8
    for command in named:
        assert command_lines.taken(command), command


def test_the_lineage_page_names_the_stop_gate_by_the_mode_names_the_command_line_prints() -> None:
    text = PAGE.read_text()
    default = modes.label(ExpertStop.default_mode)
    assert f"Its default mode is `{default}`" in text
    assert f"keeps the Stop gate at `{default}`" in text


def test_each_mode_of_the_stop_gate_is_typed_on_the_lineage_page_as_the_shell_takes_it() -> None:
    typed = re.findall(rf"`agentihooks swarm <id> set ({SETTING}=[^`]+)`", PAGE.read_text())
    settings = [shlex.split(setting) for setting in typed]
    assert settings == [[f"{SETTING}={modes.label(mode)}"] for mode in modes.MODES]
    for (setting,) in settings:
        key, _, value = setting.partition("=")
        assert cli.gate_mode(key, value) == {ExpertStop.name: modes.normalize(value)}


def test_the_lineage_page_marks_what_it_describes_before_it_is_built() -> None:
    assert "## Not yet built" in PAGE.read_text()
