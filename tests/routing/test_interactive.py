import subprocess
import sys
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
    home = tmp_path / "profile"
    home.mkdir()
    credentials = home / ".credentials.json"
    credentials.symlink_to(tmp_path / "login")
    (tmp_path / "login").touch()
    source = {**dict.fromkeys(CODEX_DROP, "sentinel"), "CLAUDE_CONFIG_DIR": str(home)}
    monkeypatch.setattr(master_account, "load", lambda env: {harness: declared} if env is source else {})
    monkeypatch.setattr(interactive.shutil, "which", {"codex": "/opt/codex"}.get)
    run = Mock(return_value=SimpleNamespace(returncode=0))
    if harness == "claude":
        run.side_effect = AssertionError("a claude login check must not run codex")
    assert interactive.account(harness, source, run) == declared
    assert credentials.is_symlink()
    if harness == "codex":
        run.assert_called_once_with(
            ["/opt/codex", "login", "status"],
            env=envs.codex_interactive_child(source, "master"),
            capture_output=True,
            timeout=30,
        )
        assert not any(key in run.call_args.kwargs["env"] for key in CODEX_DROP)
    credentials.unlink()
    run.return_value.returncode = 1
    error = ClaudeError if harness == "claude" else CodexError
    with pytest.raises(error, match="^interactive login missing$") as raised:
        interactive.account(harness, source, run)
    assert type(raised.value) is error


@pytest.mark.parametrize("failure", [OSError("no codex"), subprocess.TimeoutExpired(["codex"], 30)])
def test_codex_login_check_failure_reads_as_missing_login(monkeypatch, failure):
    from scripts.codex_router import RoutingError
    from scripts.routing import interactive

    monkeypatch.setattr(master_account, "load", lambda _: {"codex": master_account.MasterAccount("codex", "master")})
    monkeypatch.setattr(interactive.shutil, "which", lambda _: None)
    run = Mock(side_effect=failure)
    with pytest.raises(RoutingError, match="^interactive login missing$"):
        interactive.account("codex", {}, run)
    assert run.call_args.args[0] == ["codex", "login", "status"]


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


class _Stdout:
    def __init__(self):
        self.text, self.flushed = "", ""

    def write(self, text):
        self.text += text

    def flush(self):
        self.flushed = self.text


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_interactive_launch_routes_without_reading_subscription_quota(tmp_path, monkeypatch, harness):
    from scripts import codex_router, install
    from scripts.routing import interactive

    report = tmp_path / "report"
    source = {**dict.fromkeys(CLAUDE_DROP + CODEX_DROP, "sentinel"), "HOME": str(tmp_path)}
    checked, executed, out = [], [], _Stdout()

    def account(kind, env, *rest):
        checked.append((kind, env.get("AH_CC_TOKEN_one"), rest))
        return master_account.MasterAccount(kind, "master")

    def execute(binary, argv, child):
        executed.append((binary, argv, child, out.flushed))

    monkeypatch.setattr(interactive, "account", account)
    monkeypatch.setattr(install.shutil, "which", lambda name: f"/opt/{name}")
    monkeypatch.setattr(sys, "stdout", out)
    argv = ["--route", "interactive", "--agentihooks-report", str(report), "--version"]
    if harness == "codex":
        run = Mock()
        assert codex_router.main(argv, source, execute, run) == 0
        assert checked == [("codex", "sentinel", (run,))]
        line = "[agentihooks codex] account=master sessions=0/? placement=forced"
    else:
        monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
        monkeypatch.setattr("scripts.deps_preflight.ensure", lambda: None)
        monkeypatch.setattr("scripts.claude_quota_balancer._cache_path", lambda _: tmp_path / "quota.json")
        for name, value in source.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(install.os, "execvpe", execute)
        install.cmd_claude(argv)
        assert checked == [("claude", "sentinel", ())]
        line = "[agenti] account=master route=interactive"
    binary, command, child, flushed = executed[0]
    assert binary == command[0] == f"/opt/{harness}"
    assert "--route" not in command
    assert command[-1] == "--version"
    assert line in flushed
    assert report.read_text().splitlines() == ["status=routed", "account=master", "placement=forced"]
    assert child["AH_ROUTE_INTERACTIVE_master"] == "1"
    assert child["HOME"] == str(tmp_path)
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


def test_init_agent_auth_option_accepts_only_interactive():
    from scripts import init_agent

    parser = init_agent._parser()
    assert parser.parse_args([]).auth == ""
    with pytest.raises(SystemExit):
        parser.parse_args(["--auth", "token"])
    assert "{interactive} Use the declared interactive login --profile" in " ".join(parser.format_help().split())


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
    assert "agent_reason=interactive" in output.splitlines()
    choose.assert_not_called()


@pytest.mark.parametrize(
    "child", [envs.subscription_child, envs.api_child, envs.codex_subscription_child, envs.codex_api_child]
)
def test_other_route_kinds_drop_inherited_interactive_markers(child):
    assert "AH_ROUTE_INTERACTIVE_old" not in child({"AH_ROUTE_INTERACTIVE_old": "1", "PATH": "/bin"})
