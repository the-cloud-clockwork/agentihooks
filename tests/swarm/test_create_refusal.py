"""Swarm create runs the store's refusals before it marks the ledger as a swarm ledger."""

import argparse
import os
from dataclasses import replace

import pytest

from scripts.swarm import cli, create_config, templates
from scripts.swarm.store import APP_SERVER_TRANSPORT, DEFAULT_MASTERS_STEP, DELEGATE, EVENTS_ENGINE, FULL, SwarmConfig
from scripts.swarm_ledger import ledger_creator
from tests.swarm.test_cli import env as env


def test_a_create_the_store_refuses_leaves_the_ledger_unmarked(env: tuple, capsys: pytest.CaptureFixture) -> None:
    _, ledger, _ = env
    assert cli.main(["sw", "create", "--repo", "/repo"]) == 0
    ledger.swarm_sized.clear()
    capsys.readouterr()
    assert cli.main(["sw", "create", "--repo", "/repo"]) == 1
    assert capsys.readouterr().err == "swarm: swarm sw already exists\n"
    assert ledger.swarm_sized == []


def test_create_refusal_names_what_create_would_refuse_before_a_ledger_is_marked(env: tuple) -> None:
    swarms, ledger, _ = env
    assert cli.create_refusal(swarms, ["sw", "create", "--repo", "/repo"]) == ""
    assert cli.create_refusal(swarms, ["2026-q4", "create", "--repo", "/repo"]) == (
        "a swarm id is lowercase letters, digits and dashes, starting with a letter, at most 48 long"
    )
    assert ledger.swarm_sized == [] and swarms.slugs() == []
    assert cli.main(["sw", "create", "--repo", "/repo"]) == 0
    assert cli.create_refusal(swarms, ["sw", "create", "--repo", "/repo"]) == "swarm sw already exists"


def _args(**given: object) -> argparse.Namespace:
    flags = {"slug": "sw", "template": "wide", "max_eng_agents": None, "max_ci_agents": None, "max_plan_agents": None}
    return argparse.Namespace(**{**flags, **given})


@pytest.fixture
def wide(monkeypatch: pytest.MonkeyPatch) -> templates.Template:
    monkeypatch.setattr("scripts.hive.daemon.origin", lambda path: f"origin of {path}")
    monkeypatch.setattr(ledger_creator, "shared", lambda environ: environ is os.environ)
    lanes = {"eng": {"cap": 5}, "ci": {"cap": 4}, "plan": {"cap": 3}}
    return templates.parse({"name": "wide", "lanes": lanes, "compact_limit": 7, "autonomy": FULL})


def _expected(template: templates.Template, *caps: int, pinned: bool, autonomy: str) -> SwarmConfig:
    eng, ci, plan = caps
    return SwarmConfig(
        "sw",
        "/repo",
        eng,
        ci,
        max_plan=plan,
        state="paused",
        compact_limit=template.compact_limit,
        template="wide",
        lanes=templates.lane_map(template),
        links=[],
        autonomy=autonomy,
        scratch=False,
        caps_pinned=pinned,
        codex_inbox_transport=APP_SERVER_TRANSPORT,
        masters_step=DEFAULT_MASTERS_STEP,
        engine=EVENTS_ENGINE,
        repo_origin="origin of /repo",
    )


def test_the_create_config_takes_template_caps_unless_a_flag_pins_them(wide: templates.Template) -> None:
    assert create_config.config(_args(), "/repo", wide) == _expected(wide, 5, 4, 3, pinned=False, autonomy=FULL)
    pinned = _args(max_eng_agents=2, max_plan_agents=6)
    assert create_config.config(pinned, "/repo", wide) == _expected(wide, 2, 4, 6, pinned=True, autonomy=FULL)
    pinned = _args(max_ci_agents=1)
    assert create_config.config(pinned, "/repo", wide) == _expected(wide, 5, 1, 3, pinned=True, autonomy=FULL)


def test_a_template_without_autonomy_creates_a_delegate_swarm(wide: templates.Template) -> None:
    plain = replace(wide, autonomy="", compact_limit=0)
    assert create_config.config(_args(), "/repo", plain) == _expected(plain, 5, 4, 3, pinned=False, autonomy=DELEGATE)
