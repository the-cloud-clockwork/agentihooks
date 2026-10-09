from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.routing import envs, master_account

CLAUDE_DROP = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "AH_CC_TOKEN_one",
    "AH_CC_TOKEN_two",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FUTURE",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_GROWTHBOOK",
    "AH_ROUTE_API",
    "AH_ROUTE_INTERACTIVE_old",
)
CODEX_DROP = (
    "CODEX_ACCESS_TOKEN",
    "AH_CX_TOKEN_one",
    "AH_CX_TOKEN_two",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "AH_ROUTE_API",
    "AH_ROUTE_INTERACTIVE_old",
)


@pytest.mark.parametrize("harness,dropped", [("claude", CLAUDE_DROP), ("codex", CODEX_DROP)])
def test_interactive_environment_scrubs_overrides_and_keeps_profile_home(harness, dropped):
    source = dict.fromkeys(dropped, "sentinel")
    source.update(PATH="/bin", CLAUDE_CONFIG_DIR="/profile", CODEX_HOME="/profile-codex")
    child = getattr(envs, f"{harness}_interactive_child")(source, "master")
    assert child == {
        "PATH": "/bin",
        "CLAUDE_CONFIG_DIR": "/profile",
        "CODEX_HOME": "/profile-codex",
        "AH_ROUTE_INTERACTIVE_master": "1",
    }
    assert all(name in source for name in dropped)


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_interactive_login_presence_and_missing_login(tmp_path, monkeypatch, harness):
    from scripts.claude_quota_balancer import RoutingError as ClaudeError
    from scripts.codex_router import RoutingError as CodexError
    from scripts.routing import interactive

    declared = master_account.MasterAccount(harness, "master")
    monkeypatch.setattr(master_account, "load", lambda _: {harness: declared})
    home = tmp_path / "profile"
    home.mkdir()
    credentials = home / ".credentials.json"
    credentials.symlink_to(tmp_path / "login")
    (tmp_path / "login").touch()
    source = {**dict.fromkeys(CODEX_DROP, "sentinel"), "CLAUDE_CONFIG_DIR": str(home)}
    run = Mock(return_value=SimpleNamespace(returncode=0))
    assert interactive.account(harness, source, run) == declared
    assert credentials.is_symlink()
    if harness == "codex":
        args, kwargs = run.call_args
        assert args[0][-2:] == ["login", "status"]
        assert not any(key in kwargs["env"] for key in CODEX_DROP)
    credentials.unlink()
    run.return_value.returncode = 1
    with pytest.raises(ClaudeError if harness == "claude" else CodexError, match="^interactive login missing$"):
        interactive.account(harness, source, run)


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_interactive_route_requires_a_declaration(monkeypatch, harness):
    from scripts.claude_quota_balancer import RoutingError as ClaudeError
    from scripts.codex_router import RoutingError as CodexError
    from scripts.routing import interactive

    monkeypatch.setattr(master_account, "load", lambda _: {})
    with pytest.raises(
        ClaudeError if harness == "claude" else CodexError, match="^interactive master account missing$"
    ):
        interactive.account(harness, {})


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_interactive_launch_routes_without_reading_subscription_quota(tmp_path, monkeypatch, harness):
    from scripts import codex_router, install
    from scripts.routing import interactive

    source = {**dict.fromkeys(CLAUDE_DROP + CODEX_DROP, "sentinel"), "HOME": str(tmp_path)}
    monkeypatch.setattr(interactive, "account", lambda kind, env, *args: master_account.MasterAccount(kind, "master"))
    executed = []

    def execute(binary, argv, child):
        executed.append((binary, argv, child))

    if harness == "codex":
        assert codex_router.main(["--route", "interactive", "--version"], source, execute) == 0
    else:
        monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
        monkeypatch.setattr("scripts.deps_preflight.ensure", lambda: None)
        monkeypatch.setattr("scripts.claude_quota_balancer._cache_path", lambda _: tmp_path / "quota.json")
        monkeypatch.setattr(install.os, "environ", source)
        monkeypatch.setattr(install.os, "execvpe", execute)
        install.cmd_claude(["--route", "interactive", "--version"])
    _, argv, child = executed[0]
    assert "--route" not in argv
    assert argv[-1] == "--version"
    assert child["AH_ROUTE_INTERACTIVE_master"] == "1"
    assert all(name not in child for name in (CLAUDE_DROP if harness == "claude" else CODEX_DROP))


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_init_agent_explicit_interactive_auth_routes_the_selected_harness(tmp_path, harness):
    from scripts import init_agent

    parsed = init_agent._parser().parse_args(["--auth", "interactive"])
    assert parsed.auth == "interactive"
    command, _ = init_agent._agent_command(
        init_agent.AgentSpec(agent=harness, auth=parsed.auth), Path("/report"), "probe", [], {}, tmp_path
    )
    assert command[command.index("--route") + 1] == "interactive"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_interactive_marker_attribution_agrees_with_process_binding(tmp_path, harness):
    from hooks.context.account_sessions import account_from_names, codex_account_from_names
    from scripts.profiles import binding

    marker = "AH_ROUTE_INTERACTIVE_master"
    assert account_from_names([marker]) == "master"
    assert codex_account_from_names([marker]) == "master"
    agent = tmp_path / "2"
    agent.mkdir()
    (agent / "comm").write_text(harness)
    (agent / "environ").write_bytes(f"{marker}=1\0{binding.HOMES[harness]}=/profile".encode())
    (agent / "cmdline").write_bytes(harness.encode())
    assert binding.process(tmp_path, 2)[3] == "master"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_init_agent_interactive_auth_bypasses_subscription_harness_rotation(tmp_path, monkeypatch, capsys, harness):
    from scripts import init_agent
    from tests.test_init_agent import _profile

    _, profile_env = _profile(monkeypatch, tmp_path, harness)
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    choose = Mock(side_effect=RuntimeError("subscription quota must not choose an interactive harness"))
    monkeypatch.setattr(init_agent.agent_choice, "choose", choose)
    args = ["--dir", str(tmp_path), "--auth", "interactive", "--dry-run"]
    if harness == "codex":
        args += ["--agent", "codex"]
    assert init_agent.main(args, {"XDG_RUNTIME_DIR": str(tmp_path), **profile_env}) == 0
    output = capsys.readouterr().out
    launcher = next(line.split("=", 1)[1] for line in output.splitlines() if line.startswith("launcher="))
    assert "--route interactive" in Path(launcher).read_text()
    assert f"agent={harness}" in output
    choose.assert_not_called()


@pytest.mark.parametrize(
    "child", [envs.subscription_child, envs.api_child, envs.codex_subscription_child, envs.codex_api_child]
)
def test_other_route_kinds_drop_inherited_interactive_markers(child):
    assert "AH_ROUTE_INTERACTIVE_old" not in child({"AH_ROUTE_INTERACTIVE_old": "1", "PATH": "/bin"})
