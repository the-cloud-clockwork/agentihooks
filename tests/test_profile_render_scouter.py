"""Rendered seat homes carry the packaged scouter and the scouter first step rule on both harnesses."""

import tomllib
from pathlib import Path
from typing import Any

import pytest

from scripts.install import PACKAGE_FEATURES_DIR
from scripts.profiles import render
from tests.test_profile_render import _flat, _write
from tests.test_profile_render import world as world


def _seat(world: dict[str, Any], role: str) -> None:
    _write(world["bundle"] / "profiles" / role / "profile.yml", f"name: {role}\nextends: [package:{role}]\n")


def _home(target: str, role: str) -> Path:
    out = render.render(target, role)
    assert out is not None
    return out


@pytest.mark.parametrize("role", ["engineer", "cicd", "master"])
def test_every_seat_home_carries_the_packaged_scouter_on_both_harnesses(world: dict[str, Any], role: str) -> None:
    _seat(world, role)
    packaged = PACKAGE_FEATURES_DIR / "agents" / "scouter.md"
    assert (_home("claude", role) / "agents" / "scouter.md").resolve() == packaged.resolve()
    agent = tomllib.loads((_home("codex", role) / "agents" / "scouter.toml").read_text())
    assert sorted(agent) == ["description", "developer_instructions", "name"]
    assert agent["name"] == "scouter"
    assert agent["description"].startswith("Answers one question with an evidence set gathered")
    assert agent["developer_instructions"].startswith("You answer one question with an evidence set.")
    assert agent["developer_instructions"].endswith("Never present a guess as a claim.")


@pytest.mark.parametrize(
    "front",
    ["name: bare", "name: bare\ndescription: [a, b]", "name: bare\ndescription: '  '", "description: x: y"],
)
def test_a_codex_home_skips_an_agent_without_a_string_description(world: dict[str, Any], front: str) -> None:
    _seat(world, "engineer")
    _write(world["bundle"] / ".claude" / "agents" / "bare.md", f"---\n{front}\n---\nBody only.\n")
    assert sorted(p.name for p in (_home("codex", "engineer") / "agents").iterdir()) == ["scouter.toml"]


@pytest.mark.parametrize(
    "front",
    [
        "description: Plain helper.",
        "name:\ndescription: Plain helper.",
        "name: 7\ndescription: Plain helper.",
        "name: '  '\ndescription: Plain helper.",
    ],
)
def test_a_codex_agent_without_a_string_name_takes_its_file_name(world: dict[str, Any], front: str) -> None:
    _seat(world, "engineer")
    _write(world["bundle"] / ".claude" / "agents" / "helper.md", f"---\n{front}\n---\nHelp.\n")
    agent = tomllib.loads((_home("codex", "engineer") / "agents" / "helper.toml").read_text())
    assert agent == {"name": "helper", "description": "Plain helper.", "developer_instructions": "Help."}


def test_a_codex_agent_keeps_a_dash_rule_inside_its_frontmatter_and_body(world: dict[str, Any]) -> None:
    _seat(world, "engineer")
    text = "---\ndescription: Runs the --- suite\n---\nStep one.\n---\nStep two.\n"
    _write(world["bundle"] / ".claude" / "agents" / "dashes.md", text)
    agent = tomllib.loads((_home("codex", "engineer") / "agents" / "dashes.toml").read_text())
    assert agent["description"] == "Runs the --- suite"
    assert agent["developer_instructions"] == "Step one.\n---\nStep two."


def test_a_codex_agent_reads_frontmatter_with_windows_line_endings(world: dict[str, Any]) -> None:
    _seat(world, "engineer")
    path = world["bundle"] / ".claude" / "agents" / "crlf.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"---\r\ndescription: Windows helper.\r\n---\r\nHelp.\r\n")
    agent = tomllib.loads((_home("codex", "engineer") / "agents" / "crlf.toml").read_text())
    assert agent == {"name": "crlf", "description": "Windows helper.", "developer_instructions": "Help."}


def test_a_codex_home_skips_an_agent_without_frontmatter(world: dict[str, Any]) -> None:
    _seat(world, "engineer")
    _write(world["bundle"] / ".claude" / "agents" / "plain.md", "Intro.\n---\ndescription: Not a header.\n---\nBody.\n")
    assert sorted(p.name for p in (_home("codex", "engineer") / "agents").iterdir()) == ["scouter.toml"]


def test_a_codex_home_skips_an_agent_link_whose_source_is_gone(world: dict[str, Any]) -> None:
    _seat(world, "engineer")
    claude = _home("claude", "engineer")
    (claude / "agents" / "gone.md").symlink_to(claude / "nowhere.md")
    assert sorted(p.name for p in (_home("codex", "engineer") / "agents").iterdir()) == ["scouter.toml"]


@pytest.mark.parametrize("role", ["engineer", "cicd"])
def test_engineer_and_ci_homes_require_the_scouter_as_the_first_step(world: dict[str, Any], role: str) -> None:
    _seat(world, role)
    rule = "Your first act on every task is launching the scouter sub agent"
    assert rule in _flat((_home("claude", role) / "CLAUDE.md").read_text())
    assert rule in _flat((_home("codex", role) / "AGENTS.md").read_text())


def test_the_master_home_carries_the_scouter_validation_step(world: dict[str, Any]) -> None:
    _seat(world, "master")
    rule = "A seat that blocks its task on a scouter finding"
    assert rule in _flat((_home("claude", "master") / "CLAUDE.md").read_text())
    assert rule in _flat((_home("codex", "master") / "AGENTS.md").read_text())
