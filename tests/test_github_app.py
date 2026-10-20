import io
from unittest.mock import Mock

import pytest


def app(monkeypatch):
    from scripts import github_app

    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fixture")
    clock = Mock(return_value=1893456000)
    monkeypatch.setattr(github_app.time, "time", clock)
    signed = Mock(return_value="fixture")
    monkeypatch.setattr(github_app.jwt, "encode", signed)
    post = Mock(
        return_value=Mock(
            status_code=201,
            json=Mock(
                return_value={
                    "token": "fixture",
                    "expires_at": "2030-01-01T01:00:00Z",
                }
            ),
        )
    )
    monkeypatch.setattr(github_app.requests, "post", post)
    return github_app, clock, signed, post


def test_installation_tokens_use_the_agenticore_exchange_and_cache_until_five_minutes_early(monkeypatch):
    module, clock, signed, post = app(monkeypatch)
    auth = module.GitHubAppAuth()
    assert auth.get_token() == "fixture"
    signed.assert_called_once_with({"iat": 1893455940, "exp": 1893456600, "iss": "123"}, "fixture", algorithm="RS256")
    post.assert_called_once_with(
        "https://api.github.com/app/installations/456/access_tokens",
        headers={"Authorization": "Bearer fixture"},
        json={},
        timeout=10,
    )
    clock.return_value += 3299
    assert auth.get_token() == "fixture"
    assert post.call_count == 1
    clock.return_value += 1
    post.return_value.json.return_value = {"token": "renewed", "expires_at": "2030-01-01T02:00:00Z"}
    assert auth.get_token() == "renewed"
    assert post.call_count == 2


def test_refused_refresh_never_returns_the_expiring_token(monkeypatch):
    module, clock, _, post = app(monkeypatch)
    auth = module.GitHubAppAuth()
    assert auth.get_token() == "fixture"
    clock.return_value += 3300
    post.return_value.status_code = 401
    with pytest.raises(module.GitAuthRefused):
        auth.get_token()


def test_worker_and_general_consumers_share_the_same_minting_class():
    from scripts import github_app
    from scripts.swarm_v2.worker import git_auth

    assert git_auth.GitHubAppAuth is github_app.GitHubAppAuth


def test_credential_helper_emits_only_for_github_https_and_never_handles_store(monkeypatch):
    from scripts import github_app_cli

    mint = Mock(return_value="fixture")
    monkeypatch.setattr(github_app_cli, "token", mint)
    output = io.StringIO()
    assert github_app_cli.credential("get", io.StringIO("protocol=https\nhost=github.com\n\n"), output) == 0
    assert output.getvalue().splitlines() == ["username=x-access-token", "password=fixture", ""]
    for operation, request in (
        ("store", "protocol=https\nhost=github.com\n\n"),
        ("erase", ""),
        ("get", "protocol=http\nhost=github.com\n\n"),
        ("get", "protocol=https\nhost=elsewhere.example\n\n"),
    ):
        output = io.StringIO()
        assert github_app_cli.credential(operation, io.StringIO(request), output) == 0
        assert output.getvalue() == ""
    mint.assert_called_once_with()


def test_token_mode_is_explicit_and_app_failure_never_falls_back(monkeypatch):
    from scripts import github_app_cli

    monkeypatch.setenv("HIVE_GITHUB_AUTH_MODE", "token")
    monkeypatch.setenv("GITHUB_TOKEN", "fixture")
    assert github_app_cli.token() == "fixture"
    monkeypatch.setenv("HIVE_GITHUB_AUTH_MODE", "app")
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    with pytest.raises(KeyError):
        github_app_cli.token()


def test_exec_refreshes_environment_without_putting_a_token_in_arguments(monkeypatch):
    from scripts import github_app_cli

    monkeypatch.setattr(github_app_cli, "token", Mock(return_value="fixture"))
    execute = Mock()
    monkeypatch.setattr(github_app_cli.os, "execvpe", execute)
    assert github_app_cli.main(["exec", "fixture-command", "argument"]) == 0
    program, arguments, env = execute.call_args.args
    assert program == "fixture-command"
    assert arguments == ["fixture-command", "argument"]
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == "fixture"
    assert env["AGENTIHOOKS_GITHUB_AUTH_READY"] == "1"
