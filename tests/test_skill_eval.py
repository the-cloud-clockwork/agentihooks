from unittest.mock import Mock

import pytest

from hooks.context import account_sessions
from scripts import claude_quota_balancer as balancer
from scripts import install, session_caps, skill_eval
from scripts.session_caps import SessionCaps


@pytest.fixture
def launch(monkeypatch):
    environ = {
        "AH_CC_TOKEN_winner": "test",
        "AH_CC_TOKEN_peer": "other",
        "ANTHROPIC_API_KEY": "api",
        "CLAUDE_CODE_OAUTH_TOKEN": "expired",
        "CLAUDECODE": "nested",
        "PATH": "/usr/bin",
    }
    if "MUTANT_UNDER_TEST" in skill_eval.os.environ:
        environ["MUTANT_UNDER_TEST"] = skill_eval.os.environ["MUTANT_UNDER_TEST"]
    monkeypatch.setattr(skill_eval.os, "environ", environ)
    loader = Mock()
    monkeypatch.setattr(install, "_load_claude_runtime_env", loader)
    monkeypatch.setattr(skill_eval.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {"winner": 1, "peer": 3})
    monkeypatch.setattr(account_sessions, "max_sessions", lambda env: 3)
    caps = SessionCaps(3, {"winner": 2})
    monkeypatch.setattr(session_caps, "caps", lambda default: caps)
    result = balancer.ProbeResult(
        account="winner",
        provider_status="allowed",
        state="NORMAL",
        margin=70,
        five_hour=balancer.QuotaWindow(used=20),
        seven_day=balancer.QuotaWindow(used=30),
    )
    selector = Mock(
        return_value=balancer.RouteDecision(
            balancer.Credential("AH_CC_TOKEN_winner", environ["AH_CC_TOKEN_winner"]),
            result,
            "cached",
        )
    )
    monkeypatch.setattr(balancer, "select_credential", selector)
    execute = Mock(side_effect=RuntimeError("exec intercepted"))
    monkeypatch.setattr(skill_eval.os, "execvpe", execute)
    return environ, loader, selector, execute, caps


def test_claude_evaluation_routes_without_default_login(launch, monkeypatch, tmp_path, capsys):
    environ, loader, selector, execute, caps = launch
    environ.pop("CLAUDE_CODE_OAUTH_TOKEN")
    environ.pop("PYTEST_CURRENT_TEST")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    command = ["python3", "-m", "scripts.run_eval", "--model", "haiku"]

    with pytest.raises(RuntimeError, match="exec intercepted"):
        skill_eval.main(["--", *command])

    loader.assert_called_once_with()
    selector.assert_called_once_with(
        environ,
        include_fable=False,
        claude_bin="/usr/bin/claude",
        sessions={"winner": 1, "peer": 3},
        caps=caps,
    )
    executable, argv, child = execute.call_args.args
    assert executable == "python3"
    assert argv == command
    assert child == {
        **{name: value for name, value in environ.items() if name == "MUTANT_UNDER_TEST"},
        "AH_CC_TOKEN_winner": "test",
        "CLAUDE_CODE_OAUTH_TOKEN": "test",
        "AGENTIHOOKS_ROUTE_ACCOUNT": "winner",
        "CLAUDE_CONFIG_DIR": str(tmp_path),
        "PATH": "/usr/bin",
    }
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "[skill-eval] account=winner\n"


def test_claude_evaluation_replaces_expired_auth_and_checks_model(launch, capsys):
    _, _, selector, execute, _ = launch
    with pytest.raises(RuntimeError, match="exec intercepted"):
        skill_eval.main(["--agent", "claude", "claude", "-p", "evaluate", "--model=fable"])
    assert selector.call_args.kwargs["include_fable"] is True
    assert execute.call_args.args[2]["CLAUDE_CODE_OAUTH_TOKEN"] == "test"
    assert capsys.readouterr().err == "[skill-eval] account=winner\n"


def test_codex_evaluation_keeps_command_and_environment(launch, capsys):
    environ, loader, selector, execute, _ = launch
    command = ["codex", "exec", "evaluate", "--model", "luna"]
    with pytest.raises(RuntimeError, match="exec intercepted"):
        skill_eval.main(["--agent", "codex", "--", *command])
    execute.assert_called_once_with("codex", command, environ)
    loader.assert_not_called()
    selector.assert_not_called()
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_evaluation_refuses_unroutable_claude(launch, capsys):
    _, _, selector, execute, _ = launch
    selector.side_effect = balancer.RoutingError("no Claude account has verified routing capacity")
    with pytest.raises(SystemExit) as error:
        skill_eval.main(["claude", "-p", "evaluate"])
    assert error.value.code == 3
    execute.assert_not_called()
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "skill-eval: no Claude account has verified routing capacity\n"


@pytest.mark.parametrize("args", [[], ["--"], ["--agent", "codex", "--"]])
def test_evaluation_requires_a_command(args, launch, capsys):
    _, loader, selector, execute, _ = launch
    with pytest.raises(SystemExit) as error:
        skill_eval.main(args)
    assert error.value.code == 2
    assert "an evaluation command is required after --" in capsys.readouterr().err
    loader.assert_not_called()
    selector.assert_not_called()
    execute.assert_not_called()
