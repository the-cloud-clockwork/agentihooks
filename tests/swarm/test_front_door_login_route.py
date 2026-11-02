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


@pytest.mark.parametrize("pin", ["lane", "affinity", "saved"])
@pytest.mark.parametrize("seat", ["master@proof", None])
def test_front_door_login_precedes_token_choice_for_every_harness_source(tmp_path, monkeypatch, pin, seat):
    from hooks import config as hooks_config

    monkeypatch.setattr(hooks_config, "MASTER_CLAUDE_LOGIN", False)
    monkeypatch.setattr(runtime.affinity, "desired", lambda _: "claude" if pin == "affinity" else "")
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runner = runtime.HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: pytest.fail("token choice"))
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"} if pin == "lane" else {}},
        autonomy="delegate",
    )
    task = {"id": "master", "route_kind": "subscription"}
    if seat is not None:
        task["seat"] = seat
    if pin == "saved":
        task["handoff_envelope"] = {
            "launch": {"profile": "master", "harness": "claude", "account": "old-token"},
        }
    placed = runner.spawn(config, "master", "master@next", task)
    assert _passed(seen[0])[:2] == ["--route", "claude-ai"]
    assert placed.route_kind == "interactive"
    assert placed.remote_control == {"state": "on", "detail": "master 1 · proof"}


def test_codex_affinity_keeps_subscription_routing_with_native_claude_available(tmp_path, monkeypatch):
    from scripts.routing import native_login

    monkeypatch.setattr(runtime.affinity, "desired", lambda _: "codex")
    monkeypatch.setattr(native_login, "check", lambda *_: dict(native_login.PROOF))
    monkeypatch.setattr(runtime.HerdrRuntime, "_master_account_spent", lambda self: False)
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runner = runtime.HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("codex", "requested"))
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {}},
        autonomy="delegate",
    )
    placed = runner.spawn(config, "master", "master@next", {"id": "master", "seat": "master@proof"})
    assert "--route" not in _passed(seen[0])
    assert placed.harness == "codex"
    assert placed.route_kind == "subscription"


def test_custom_master_profile_keeps_the_existing_opt_in_login(tmp_path, monkeypatch):
    from hooks import config as hooks_config
    from scripts.routing import native_login

    monkeypatch.setattr(native_login, "check", lambda *_: dict(native_login.PROOF))
    monkeypatch.setattr(hooks_config, "MASTER_CLAUDE_LOGIN", True)
    monkeypatch.setattr(runtime.HerdrRuntime, "_master_account_spent", lambda self: False)
    monkeypatch.setattr(runtime.affinity, "desired", lambda _: "")
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runner = runtime.HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: pytest.fail("token choice"))
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {}},
        autonomy="delegate",
    )
    placed = runner.spawn(
        config,
        "master",
        "master@next",
        {
            "id": "master",
            "seat": "master@proof",
            "route_kind": "subscription",
            "handoff_envelope": {"launch": {"profile": "engineer", "harness": "claude"}},
        },
    )
    assert _passed(seen[0])[:2] == ["--route", "interactive"]
    assert placed.route_kind == "interactive"
