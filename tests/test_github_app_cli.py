import io
from unittest.mock import Mock

import pytest

from scripts import github_app_cli

pytestmark = pytest.mark.unit


def test_app_mode_defaults_to_the_shared_in_memory_cache(monkeypatch):
    monkeypatch.delenv("HIVE_GITHUB_AUTH_MODE", raising=False)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "pem")
    monkeypatch.setattr(github_app_cli, "_AUTH", None)
    auth = Mock()
    auth.get_token.side_effect = ["fixture", "renewed"]
    factory = Mock(return_value=auth)
    monkeypatch.setattr(github_app_cli, "GitHubAppAuth", factory)
    assert github_app_cli.token() == "fixture"
    assert github_app_cli.token() == "renewed"
    factory.assert_called_once_with()
    assert auth.get_token.call_count == 2


@pytest.mark.parametrize("mode,expected", [("token", github_app_cli.GitAuthRefused), ("other", ValueError)])
def test_empty_selected_token_and_unknown_mode_are_refused(monkeypatch, mode, expected):
    monkeypatch.setenv("HIVE_GITHUB_AUTH_MODE", mode)
    monkeypatch.setenv("GITHUB_TOKEN", "")
    with pytest.raises(expected):
        github_app_cli.token()


def test_process_environment_overrides_stored_helpers_and_preserves_existing_git_settings(monkeypatch):
    monkeypatch.setattr(github_app_cli, "token", Mock(return_value="fixture"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    env = github_app_cli.environment()
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == "fixture"
    assert env["AGENTIHOOKS_GITHUB_AUTH_READY"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_0"] == "core.bare"
    assert env["GIT_CONFIG_VALUE_0"] == "false"
    assert env["GIT_CONFIG_KEY_1"] == env["GIT_CONFIG_KEY_2"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_1"] == ""
    assert env["GIT_CONFIG_VALUE_2"] == f"!{github_app_cli.sys.executable} -P -m scripts.github_app_cli credential"


def test_cli_token_prints_only_the_selected_token(monkeypatch, capsys):
    monkeypatch.setattr(github_app_cli, "token", Mock(return_value="fixture"))
    assert github_app_cli.main(["token"]) == 0
    assert capsys.readouterr() == ("fixture\n", "")


def test_cli_helper_dispatches_git_protocol_to_standard_streams(monkeypatch, capsys):
    monkeypatch.setattr(github_app_cli, "token", Mock(return_value="fixture"))
    monkeypatch.setattr(github_app_cli.sys, "stdin", io.StringIO("protocol=https\nhost=github.com\n\n"))
    assert github_app_cli.main(["credential", "get"]) == 0
    output = capsys.readouterr()
    assert output.out.splitlines() == ["username=x-access-token", "password=fixture", ""]
    assert output.err == ""


@pytest.mark.parametrize(
    "error", [KeyError, ValueError, github_app_cli.jwt.PyJWTError, github_app_cli.requests.RequestException]
)
def test_cli_refusals_never_print_error_details(monkeypatch, capsys, error):
    monkeypatch.setattr(github_app_cli, "token", Mock(side_effect=error("private fixture")))
    assert github_app_cli.main(["token"]) == 1
    assert capsys.readouterr() == ("", github_app_cli.REFUSED + "\n")


@pytest.mark.parametrize("arguments", [[], ["other"], ["credential", "other"], ["exec"]])
def test_cli_refuses_missing_commands_and_unknown_operations(arguments, capsys):
    with pytest.raises(SystemExit) as refused:
        github_app_cli.main(arguments)
    assert refused.value.code == 2
    assert capsys.readouterr().out == ""


def test_cli_is_registered_through_the_standard_delegate():
    from scripts.cli_delegates import delegated_cli

    assert delegated_cli(["github-app", "token"]) is github_app_cli.main


def test_empty_selected_token_reports_only_the_fixed_refusal(monkeypatch):
    monkeypatch.setenv("HIVE_GITHUB_AUTH_MODE", "token")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    with pytest.raises(github_app_cli.GitAuthRefused) as refused:
        github_app_cli.token()
    assert str(refused.value) == github_app_cli.REFUSED


def test_unknown_mode_names_the_two_valid_modes(monkeypatch):
    monkeypatch.setenv("HIVE_GITHUB_AUTH_MODE", "other")
    with pytest.raises(ValueError) as refused:
        github_app_cli.token()
    assert str(refused.value) == "GitHub authentication mode must be token or app"


def helper_output(monkeypatch, request_text):
    mint = Mock(return_value="fixture")
    monkeypatch.setattr(github_app_cli, "token", mint)
    output = io.StringIO()
    assert github_app_cli.credential("get", io.StringIO(request_text), output) == 0
    return output.getvalue(), mint


@pytest.mark.parametrize(
    "request_text",
    [
        "protocol=https\nhost=github.com \n\n",
        "protocol=https\nhost=github.comX\n\n",
        "protocol=https\nhost=github.com\nhost=github.com=evil\n\n",
    ],
)
def test_credential_helper_matches_exact_host_values_only(monkeypatch, request_text):
    output, mint = helper_output(monkeypatch, request_text)
    assert output == ""
    mint.assert_not_called()


def test_credential_helper_accepts_values_that_contain_equals(monkeypatch):
    plain, _ = helper_output(monkeypatch, "protocol=https\nhost=github.com\n\n")
    extra, _ = helper_output(monkeypatch, "protocol=https\nhost=github.com\nwwwauth[]=Basic realm=GitHub\n\n")
    assert plain.startswith("username=x-access-token\n")
    assert extra == plain


@pytest.mark.parametrize("operation", ["store", "erase"])
def test_cli_helper_accepts_store_and_erase_without_output(monkeypatch, capsys, operation):
    mint = Mock(return_value="fixture")
    monkeypatch.setattr(github_app_cli, "token", mint)
    monkeypatch.setattr(github_app_cli.sys, "stdin", io.StringIO("protocol=https\nhost=github.com\n\n"))
    assert github_app_cli.main(["credential", operation]) == 0
    assert capsys.readouterr() == ("", "")
    mint.assert_not_called()


def test_cli_help_describes_each_command(capsys):
    with pytest.raises(SystemExit) as shown:
        github_app_cli.main(["--help"])
    assert shown.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    for text in (
        "Use GitHub App installation credentials without saving tokens",
        "Print a current installation token for scripts",
        "Git HTTPS credential helper",
        "Refresh GitHub credentials and execute a process",
    ):
        assert text in out


def test_exec_without_a_command_names_the_missing_command(capsys):
    with pytest.raises(SystemExit):
        github_app_cli.main(["exec"])
    assert capsys.readouterr().err.rstrip().endswith("error: exec requires a command")


def test_sessions_without_the_app_key_read_the_refreshed_token_file(monkeypatch, tmp_path):
    monkeypatch.delenv("HIVE_GITHUB_AUTH_MODE", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    factory = Mock()
    monkeypatch.setattr(github_app_cli, "GitHubAppAuth", factory)
    (tmp_path / "github-token").write_text("fixture\n")
    assert github_app_cli.token() == "fixture"
    factory.assert_not_called()


def test_token_file_defaults_to_the_agentihooks_home(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTIHOOKS_HOME", raising=False)
    monkeypatch.setattr(github_app_cli.Path, "home", Mock(return_value=tmp_path))
    assert github_app_cli.token_file() == tmp_path / ".agentihooks" / "github-token"


def test_an_empty_token_file_is_refused(monkeypatch, tmp_path):
    monkeypatch.delenv("HIVE_GITHUB_AUTH_MODE", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    (tmp_path / "github-token").write_text("\n")
    with pytest.raises(github_app_cli.GitAuthRefused) as refused:
        github_app_cli.token()
    assert str(refused.value) == github_app_cli.REFUSED


def test_a_missing_token_file_is_a_quiet_refusal(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("HIVE_GITHUB_AUTH_MODE", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    assert github_app_cli.main(["token"]) == 1
    assert capsys.readouterr() == ("", github_app_cli.REFUSED + "\n")


def renewal(monkeypatch, tmp_path, mode="app", key=True):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("HIVE_GITHUB_AUTH_MODE", mode)
    if key:
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "pem")
    else:
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(github_app_cli, "_AUTH", Mock(get_token=Mock(return_value="cached")))
    mint = Mock(return_value="fresh")
    factory = Mock(return_value=Mock(get_token=mint))
    monkeypatch.setattr(github_app_cli, "GitHubAppAuth", factory)
    return tmp_path / "github-token", mint


def test_renewal_rewrites_the_private_token_file_once_thirty_minutes_pass(monkeypatch, tmp_path):
    path, mint = renewal(monkeypatch, tmp_path)
    assert github_app_cli.renew(100.0, 100.0 + github_app_cli.RENEW_SECONDS - 1) == 100.0
    mint.assert_not_called()
    assert github_app_cli.renew(100.0, 100.0 + github_app_cli.RENEW_SECONDS) == 100.0 + github_app_cli.RENEW_SECONDS
    assert path.read_text() == "fresh\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_name("github-token.next").exists()


@pytest.mark.parametrize("mode,key", [("token", True), ("app", False)])
def test_renewal_needs_app_mode_and_the_app_key(monkeypatch, tmp_path, mode, key):
    path, mint = renewal(monkeypatch, tmp_path, mode, key)
    assert github_app_cli.renew(0.0, 99999.0) == 0.0
    mint.assert_not_called()
    assert not path.exists()


def test_a_failed_renewal_keeps_the_old_file_and_retries(monkeypatch, tmp_path):
    path, mint = renewal(monkeypatch, tmp_path)
    path.write_text("old\n")
    mint.side_effect = github_app_cli.GitAuthRefused(github_app_cli.REFUSED)
    assert github_app_cli.renew(0.0, 99999.0) == 0.0
    assert path.read_text() == "old\n"
