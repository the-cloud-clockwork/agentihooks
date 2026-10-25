import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.routing import envs, native_login


@pytest.mark.parametrize(
    "payload", ["Login: Expired — log in again", "API key: managed key", "", "Login method: Console account"]
)
def test_invalid_native_login_is_refused(payload):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=payload))
    with pytest.raises(native_login.LoginError, match="native Claude login is expired or missing"):
        native_login.check({}, "master", run)


@pytest.mark.parametrize("method", ["Claude account", "Claude Max account", "Claude Pro account"])
def test_claude_subscription_login_methods_pass(method):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=f"Login method: {method}\nEmail: private"))
    assert native_login.check({}, "master", run) == native_login.PROOF


@pytest.mark.parametrize("failure", [OSError("missing"), subprocess.TimeoutExpired("claude", 30)])
def test_unavailable_status_is_refused(failure):
    with pytest.raises(native_login.LoginError, match="native Claude login could not be checked"):
        native_login.check({}, "master", Mock(side_effect=failure))


@pytest.mark.parametrize("binary", ["/opt/claude", None])
def test_status_is_redacted_and_uses_native_environment(monkeypatch, binary):
    which = Mock(side_effect={"claude": binary}.get)
    monkeypatch.setattr(native_login.shutil, "which", which)
    source = dict.fromkeys(["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"], "sentinel")
    source["HOME"] = "/native"
    payload = "Login method: Claude account\nEmail: private\nOrganization: private"
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=payload))
    proof = native_login.check(source, "master", run)
    which.assert_called_once_with("claude")
    assert proof == {"command": "claude auth status --text", "status": "Login method: Claude account"}
    run.assert_called_once_with(
        [binary or "claude", "auth", "status", "--text"],
        env=envs.claude_interactive_child(source, "master"),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_first_successful_spawn_proof_is_kept(tmp_path):
    tmp_path = tmp_path / "nested" / "home"
    first = {"command": "claude auth status --text", "status": "Login method: Claude account"}
    native_login.record(tmp_path, "proof", first)
    native_login.record(tmp_path, "proof", {**first, "authMethod": "changed"})
    assert json.loads((tmp_path / "proof" / "interactive-claude-status.json").read_text()) == first


@pytest.mark.parametrize("valid", [True, False])
def test_interactive_account_preserves_status_marker_and_refusal(tmp_path, monkeypatch, valid):
    from scripts.claude_quota_balancer import RoutingError
    from scripts.routing import interactive, master_account

    (tmp_path / ".credentials.json").touch()
    source = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    monkeypatch.setattr(master_account, "load", lambda _: {"claude": master_account.MasterAccount("claude", "master")})
    run = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout="Login method: Claude account" if valid else "Login: Expired — log in again",
        )
    )
    if valid:
        assert interactive.account("claude", source, run).slug == "master"
    else:
        with pytest.raises(
            RoutingError, match="^native Claude login is expired or missing; renew the native Claude login$"
        ) as raised:
            interactive.account("claude", source, run)
        assert isinstance(raised.value.__cause__, native_login.LoginError)
    assert run.call_args.kwargs["env"]["AH_ROUTE_INTERACTIVE_master"] == "1"
