"""Swarm create runs the store's refusals before it marks the ledger as a swarm ledger."""

import argparse
import os
from dataclasses import replace

import pytest

from scripts.swarm import cli, templates
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


def _args(**given: object) -> argparse.Namespace:
    flags = {"slug": "sw", "repo": "/repo", "template": "wide"}
    return argparse.Namespace(
        **{**flags, "max_eng_agents": None, "max_ci_agents": None, "max_plan_agents": None, **given}
    )


@pytest.fixture
def wide(monkeypatch: pytest.MonkeyPatch) -> templates.Template:
    lanes = {"eng": {"cap": 5}, "ci": {"cap": 4}, "plan": {"cap": 3}}
    template = templates.parse({"name": "wide", "lanes": lanes, "compact_limit": 7, "autonomy": FULL})
    monkeypatch.setattr(cli.templates, "load", lambda name, environ: template if name == "wide" else None)
    monkeypatch.setattr("scripts.hive.daemon.origin", lambda path: f"origin of {path}")
    monkeypatch.setattr(ledger_creator, "shared", lambda environ: environ is os.environ)
    return template


def _expected(template: templates.Template, *caps: int, pinned: bool) -> SwarmConfig:
    eng, ci, plan = caps
    return SwarmConfig(
        "sw",
        "/repo",
        eng,
        ci,
        max_plan=plan,
        state="paused",
        compact_limit=7,
        template="wide",
        lanes=templates.lane_map(template),
        links=[],
        autonomy=template.autonomy or DELEGATE,
        scratch=False,
        caps_pinned=pinned,
        codex_inbox_transport=APP_SERVER_TRANSPORT,
        masters_step=DEFAULT_MASTERS_STEP,
        engine=EVENTS_ENGINE,
        repo_origin="origin of /repo",
    )


def test_the_create_config_takes_template_caps_unless_a_flag_pins_them(wide: templates.Template) -> None:
    assert cli.create_config(_args()) == _expected(wide, 5, 4, 3, pinned=False)
    assert cli.create_config(_args(max_eng_agents=2, max_plan_agents=6)) == _expected(wide, 2, 4, 6, pinned=True)
    assert cli.create_config(_args(max_ci_agents=1)) == _expected(wide, 5, 1, 3, pinned=True)


def test_a_template_without_autonomy_creates_a_delegate_swarm(
    wide: templates.Template, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = replace(wide, autonomy="")
    monkeypatch.setattr(cli.templates, "load", lambda name, environ: plain)
    assert cli.create_config(_args()) == _expected(plain, 5, 4, 3, pinned=False)
    assert cli.create_config(_args()).autonomy == DELEGATE
