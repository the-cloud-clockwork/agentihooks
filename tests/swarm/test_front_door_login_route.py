from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.swarm import runtime
from tests.swarm.test_runtime import _passed, _resuming, validated


@pytest.mark.parametrize("spent", [False, True])
def test_front_door_ignores_token_capacity_and_forces_login(tmp_path, monkeypatch, spent):
    seen = []
    monkeypatch.setattr(runtime.HerdrRuntime, "_master_account_spent", lambda self: spent)

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runner = runtime.HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: pytest.fail("token choice"))
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )
    placed = runner.spawn(
        config,
        "master",
        "master@next",
        {"id": "master", "title": "proof", "seat": "master@proof", "route_kind": "subscription"},
    )
    assert _passed(seen[0])[:2] == ["--route", "claude-ai"]
    assert placed.route_kind == "interactive"
    assert placed.remote_control == {"state": "on", "detail": "master 1 · proof"}


def test_front_door_resume_replaces_its_previous_token_route(tmp_path):
    runner, config, agent, seen = _resuming(tmp_path, "c0ffee", "claude")
    agent = replace(
        agent,
        lane="master",
        profile="master",
        seat=f"master@{config.slug}",
        account="worker",
        route_kind="subscription",
    )
    placed = runner.resume(config, agent, "resume")
    assert _passed(seen["runs"][0])[:2] == ["--route", "claude-ai"]
    assert placed.route_kind == "interactive"
    assert placed.remote_control == {"state": "on", "detail": f"master 1 · {config.slug}"}
