from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
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


def _forced_runner(
    tmp_path: Path, lanes: dict[str, dict[str, str]], choose: Callable[..., tuple[str, str]]
) -> tuple[runtime.HerdrRuntime, SimpleNamespace, list[list[str]]]:
    seen: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runner = runtime.HerdrRuntime(home=tmp_path, run=run, choose=choose)
    config = SimpleNamespace(
        slug="proof", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes=lanes, autonomy="delegate"
    )
    return runner, config, seen


@pytest.mark.feature("forced-master-start")
@pytest.mark.parametrize(("task", "flags"), [({"forced": True}, ["--agentihooks-forced"]), ({}, [])])
def test_a_forced_front_door_spawn_hands_the_claude_ai_route_the_forced_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: dict[str, bool], flags: list[str]
) -> None:
    monkeypatch.setattr(runtime.HerdrRuntime, "_master_account_spent", lambda self: False)
    monkeypatch.setattr(runtime.affinity, "desired", lambda _: "")
    runner, config, seen = _forced_runner(tmp_path, {"master": {"agent": "claude"}}, lambda *_: pytest.fail("token"))
    runner.spawn(config, "master", "master@next", {"id": "master", "seat": "master@proof", **task})
    passed = _passed(seen[0])
    assert passed[:2] == ["--route", "claude-ai"]
    assert [flag for flag in passed if flag == "--agentihooks-forced"] == flags
    assert passed[2 : 2 + len(flags)] == flags


@pytest.mark.feature("forced-master-start")
def test_a_forced_master_off_the_interactive_route_is_never_refused_all_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.routing import native_login

    monkeypatch.setattr(runtime.affinity, "desired", lambda _: "codex")
    monkeypatch.setattr(native_login, "check", lambda *_: dict(native_login.PROOF))
    monkeypatch.setattr(runtime.HerdrRuntime, "_master_account_spent", lambda self: False)
    runner, config, seen = _forced_runner(tmp_path, {"master": {}}, lambda *_: ("codex", runtime.agent_choice.ALL_FULL))
    with pytest.raises(runtime.SpawnError) as refused:
        runner.spawn(config, "master", "master@next", {"id": "master", "seat": "master@proof"})
    assert (str(refused.value), refused.value.status, seen) == (runtime.agent_choice.ALL_FULL, "unavailable", [])
    placed = runner.spawn(config, "master", "master@next", {"id": "master", "seat": "master@proof", "forced": True})
    assert (placed.harness, "--agentihooks-forced" in _passed(seen[0])) == ("codex", False)


@pytest.mark.feature("forced-master-start")
def test_an_interactive_route_spawn_is_never_refused_all_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.routing import native_login

    monkeypatch.setattr(native_login, "check", lambda *_: dict(native_login.PROOF))
    runner, config, seen = _forced_runner(
        tmp_path, {"eng": {"agent": "claude"}}, lambda *_: ("claude", runtime.agent_choice.ALL_FULL)
    )
    placed = runner.spawn(config, "eng", "eng@a1b2c3-0001", {"id": "t1", "title": "x", "route_kind": "interactive"})
    assert (placed.harness, placed.route_kind, _passed(seen[0])[:2]) == (
        "claude",
        "interactive",
        ["--route", "interactive"],
    )
