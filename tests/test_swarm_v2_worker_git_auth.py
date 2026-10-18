import io
from datetime import datetime, timezone
from unittest.mock import Mock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from scripts.swarm_v2.worker import git_auth


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(git_auth.os, "environ", git_auth.os.environ.copy())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    return key


def response(token="installation-value", **overrides):
    data = {
        "token": token,
        "expires_at": "2030-01-01T01:00:00Z",
        "permissions": {"contents": "write"},
        "repositories": [{"full_name": "example/private"}],
        **overrides,
    }
    return Mock(status_code=201, json=Mock(return_value=data))


def test_app_mints_only_the_permitted_repository_and_refreshes_five_minutes_early(app, monkeypatch, capsys):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()
    clock = Mock(return_value=now)
    monkeypatch.setattr(git_auth.time, "time", clock)
    post = Mock(side_effect=[response(), response("refreshed-value")])
    monkeypatch.setattr(git_auth.requests, "post", post)
    auth = git_auth.GitHubAppAuth("github.com/Example/Private")
    assert auth.get_token() == "installation-value"
    assert auth.get_token() == "installation-value"
    assert post.call_count == 1
    args, kwargs = post.call_args
    assert args == ("https://api.github.com/app/installations/456/access_tokens",)
    assert kwargs["json"] == {"repositories": ["private"], "permissions": {"contents": "write"}}
    assert kwargs["timeout"] == 10
    signed = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(
        signed, app.public_key(), algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )
    assert claims == {"iat": now - 60, "exp": now + 600, "iss": "123"}
    clock.return_value = now + 3299
    assert auth.get_token() == "installation-value"
    assert post.call_count == 1
    clock.return_value = now + 3300
    assert auth.get_token() == "refreshed-value"
    assert post.call_count == 2
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "result",
    [
        Mock(status_code=403),
        response(permissions={"contents": "read"}),
        response(repositories=[{"full_name": "other/private"}]),
        response(repositories=[]),
    ],
)
def test_app_refuses_unavailable_write_permission_or_another_repository(app, monkeypatch, result):
    monkeypatch.setattr(git_auth.requests, "post", Mock(return_value=result))
    with pytest.raises(git_auth.GitAuthRefused) as refused:
        git_auth.GitHubAppAuth("github.com/example/private").get_token()
    assert str(refused.value) == "GitHub App cannot supply scoped contents write credentials"


def test_worker_sets_askpass_by_reference_and_disables_stored_helpers(app, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    git_auth.configure("github.com/Example/Private")
    import os

    assert os.environ["AGENTIHOOKS_GIT_PROJECT"] == "github.com/example/private"
    assert os.environ["GIT_ASKPASS"].endswith("/git_askpass.sh")
    assert os.environ["GIT_CONFIG_COUNT"] == "3"
    assert os.environ["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert os.environ["GIT_CONFIG_VALUE_1"] == ""
    assert os.environ["GIT_CONFIG_KEY_2"] == "credential.useHttpPath"
    assert os.environ["GIT_CONFIG_VALUE_2"] == "true"
    assert os.environ["GIT_TERMINAL_PROMPT"] == "0"


def test_askpass_returns_credentials_only_for_its_https_repository(app, monkeypatch):
    monkeypatch.setattr(git_auth.requests, "post", Mock(return_value=response()))
    git_auth.configure("github.com/Example/Private")
    output = io.StringIO()
    assert git_auth.askpass("Username for 'https://github.com/example/private.git': ", output) == 0
    assert output.getvalue() == "x-access-token\n"
    output = io.StringIO()
    assert git_auth.askpass("Password for 'https://x-access-token@github.com/example/private.git': ", output) == 0
    assert output.getvalue() == "installation-value\n"
    for prompt in (
        "Password for 'https://github.com/other/private.git': ",
        "Password for 'https://example.invalid/example/private.git': ",
        "Password for 'http://github.com/example/private.git': ",
        "Password for 'https://github.com:443/example/private.git': ",
        "Unexpected prompt",
    ):
        output = io.StringIO()
        assert git_auth.askpass(prompt, output) == 1
        assert output.getvalue() == ""


def test_askpass_command_supplies_its_output_without_logging(app, monkeypatch, capsys):
    monkeypatch.setattr(git_auth.requests, "post", Mock(return_value=response()))
    git_auth.configure("github.com/example/private")
    monkeypatch.setattr(git_auth.sys, "argv", ["askpass", "Username for 'https://github.com/example/private.git': "])
    assert git_auth.main() == 0
    assert capsys.readouterr() == ("x-access-token\n", "")


@pytest.mark.parametrize("error", [KeyError, ValueError, git_auth.requests.RequestException, jwt.PyJWTError])
def test_askpass_command_redacts_failures(app, monkeypatch, capsys, error):
    monkeypatch.setattr(git_auth, "askpass", Mock(side_effect=error("private response body")))
    monkeypatch.setattr(git_auth.sys, "argv", ["askpass", "Password"])
    assert git_auth.main() == 1
    assert capsys.readouterr() == ("", "GitHub App cannot supply scoped contents write credentials\n")
