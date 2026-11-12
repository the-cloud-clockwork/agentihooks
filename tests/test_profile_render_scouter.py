"""Rendered seat homes carry the packaged scouter and the scouter first step rule on both harnesses."""

import tomllib

import pytest

from scripts.profiles import render
from tests.test_profile_render import _flat, _write
from tests.test_profile_render import world as world


def _seat(world: dict, role: str) -> None:
    _write(world["bundle"] / "profiles" / role / "profile.yml", f"name: {role}\nextends: [package:{role}]\n")


@pytest.mark.parametrize("role", ["engineer", "cicd", "master"])
def test_every_seat_home_carries_the_packaged_scouter_on_both_harnesses(world: dict, role: str) -> None:
    _seat(world, role)
    packaged = world["install"].PACKAGE_FEATURES_DIR / "agents" / "scouter.md"
    claude = render.render("claude", role)
    assert (claude / "agents" / "scouter.md").resolve() == packaged.resolve()
    codex = render.render("codex", role)
    agent = tomllib.loads((codex / "agents" / "scouter.toml").read_text())
    assert sorted(agent) == ["description", "developer_instructions", "name"]
    assert agent["name"] == "scouter"
    assert agent["description"].startswith("Answers one question with an evidence set gathered")
    assert agent["developer_instructions"].startswith("You answer one question with an evidence set.")
    assert agent["developer_instructions"].endswith("Never present a guess as a claim.")


def test_a_codex_home_skips_an_agent_without_a_description(world: dict) -> None:
    _seat(world, "engineer")
    _write(world["bundle"] / ".claude" / "agents" / "bare.md", "---\nname: bare\n---\nBody only.\n")
    codex = render.render("codex", "engineer")
    assert sorted(p.name for p in (codex / "agents").iterdir()) == ["scouter.toml"]


@pytest.mark.parametrize("role", ["engineer", "cicd"])
def test_engineer_and_ci_homes_require_the_scouter_as_the_first_step(world: dict, role: str) -> None:
    _seat(world, role)
    claude = _flat((render.render("claude", role) / "CLAUDE.md").read_text())
    codex = _flat((render.render("codex", role) / "AGENTS.md").read_text())
    rule = "Your first act on every task is launching the scouter sub agent"
    assert rule in claude
    assert rule in codex


def test_the_master_home_carries_the_scouter_validation_step(world: dict) -> None:
    _seat(world, "master")
    claude = _flat((render.render("claude", "master") / "CLAUDE.md").read_text())
    codex = _flat((render.render("codex", "master") / "AGENTS.md").read_text())
    rule = "A seat that blocks its task on a scouter finding"
    assert rule in claude
    assert rule in codex
