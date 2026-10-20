import io
from unittest.mock import Mock

import pytest

from scripts import github_app_cli

pytestmark = pytest.mark.unit


def test_app_mode_defaults_to_the_shared_in_memory_cache(monkeypatch):
    monkeypatch.delenv("HIVE_GITHUB_AUTH_MODE", raising=False)
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
    assert env["GIT_CONFIG_VALUE_2"] == "!python3 -m scripts.github_app_cli credential"


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
