import json
import os
from pathlib import Path

import pytest

from scripts import init_agent, master_logins, openbao_kv
from scripts import master_claude_login as login
from scripts.routing import claude_ai, native_login


@pytest.mark.parametrize("route", [[], ["--route", "master-login"], ["--route", "worker"], ["--route=worker"]])
def test_front_door_command_forces_only_claude_ai(tmp_path, route):
    command, _ = init_agent._agent_command(
        init_agent.AgentSpec(profile="master", title="master 1 · rig", auth="worker"),
        tmp_path / "report",
        "master-123",
        [*route, "--model", "opus"],
        {},
        tmp_path,
    )
    assert command.count("--route") == 1
    assert command[command.index("--route") + 1] == "claude-ai"
    assert "--route=worker" not in command
    assert "--agentihooks-fallback-bare" not in command
    assert command[-2:] == ["--model", "opus"]


def test_launch_check_command_matches_the_launch_function(capsys):
    from scripts import front_door_route

    assert front_door_route.main(["--profile", "master", "--name", "master 1 · rig", "--", "--route", "worker"]) == 0
    assert json.loads(capsys.readouterr().out) == ["--route", "claude-ai"]


@pytest.mark.parametrize(
    "profile,name", [("engineer", "master 1 · rig"), ("master", "master 2 · rig"), ("master", "master-123")]
)
def test_other_launches_keep_their_route(profile, name):
    from scripts import front_door_route

    flags = ["--route", "worker", "--model", "opus"]
    assert front_door_route.check(profile, name, flags) == flags


def test_codex_keeps_its_route_under_a_master_profile():
    from scripts import front_door_route

    assert front_door_route.check("master", "master 1 · rig", ["--route", "worker"], "codex") == ["--route", "worker"]


@pytest.fixture
def shared_login(tmp_path, monkeypatch):
    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", tmp_path / "ah")
    monkeypatch.setattr(login, "MANAGED", tmp_path / "no-managed.json")
    monkeypatch.setattr(openbao_kv, "from_env", lambda _: None)
    monkeypatch.setattr(master_logins, "left", lambda *_: 80.0)
    monkeypatch.setattr(native_login, "check", lambda *_: dict(native_login.PROOF))
    monkeypatch.setenv(login.ORDER_ENV, "one@example.com,two@example.com")
    rendered = tmp_path / "profiles" / "master" / "claude"
    rendered.mkdir(parents=True)
    (rendered / "settings.json").write_text("{}")
    (rendered / login.CONFIG).write_text("{}")
    for number, email in ((1, "one@example.com"), (2, "two@example.com")):
        target = login.home(number)
        target.mkdir(parents=True)
        (target / login.CREDENTIALS).write_text(json.dumps({login.GRANT: {"refreshToken": "fixture"}}))
        (target / login.CONFIG).write_text(json.dumps({login.ACCOUNT: {"emailAddress": email}}))
        (target.parent / login.OWNER).write_text(json.dumps({"holders": {"master@old": 999999999}, "epoch": 3}))
    return {
        "CLAUDE_CONFIG_DIR": str(rendered),
        "AGENTIHOOKS_PROFILE": "master",
        "AGENTIHOOKS_AGENT_NAME": "master@successor",
        "AH_CC_TOKEN_workeratexample_com": "fixture",
        "CLAUDE_CODE_OAUTH_TOKEN": "fixture",
    }


def test_front_door_reclaims_dead_holders_and_uses_shared_login(shared_login, monkeypatch):
    executed = []
    monkeypatch.setattr("scripts.hive.account_slots.execute", lambda h, a, env, invoke: invoke(env))
    monkeypatch.setattr(claude_ai.os, "execvpe", lambda binary, argv, env: executed.append((argv, env)))
    claude_ai.launch_claude("claude", ["--name", "master 1 · rig"], shared_login, "")
    argv, env = executed[0]
    assert env["CLAUDE_CONFIG_DIR"] == str(login.home(1))
    assert login.holders(login.root(1)) == {"master@successor": os.getpid()}
    assert login._read(login.root(1) / login.OWNER)["holders"] == {"master@successor": os.getpid()}
    assert not any(key.startswith(("AH_CC_TOKEN_", "CLAUDE_CODE_OAUTH_TOKEN")) for key in env)
    assert json.loads((Path(env["CLAUDE_CONFIG_DIR"]) / login.CONFIG).read_text())[login.ACCOUNT] == {
        "emailAddress": "one@example.com"
    }
    assert argv[-2:] == ["--name", "master 1 · rig"]


def test_front_door_without_login_refuses_and_never_executes(tmp_path, monkeypatch):
    from scripts.claude_quota_balancer import RoutingError

    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", tmp_path / "ah")
    monkeypatch.setattr(login, "MANAGED", tmp_path / "no-managed.json")
    monkeypatch.setattr(openbao_kv, "from_env", lambda _: None)
    monkeypatch.setattr(login, "_offered", lambda _: {})
    monkeypatch.setattr(login, "_adopt", lambda: None)
    executed = []
    monkeypatch.setattr(claude_ai.os, "execvpe", lambda *args: executed.append(args))
    with pytest.raises(RoutingError) as refused:
        claude_ai.launch_claude(
            "claude",
            ["--name", "master 1 · rig"],
            {"AGENTIHOOKS_PROFILE": "master", "AGENTIHOOKS_AGENT_NAME": "master@next"},
            "",
        )
    assert str(refused.value) == login.NO_LOGIN
    assert executed == []
