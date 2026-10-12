import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.routing import envs, native_login


@pytest.mark.parametrize("payload", ["Login: Expired — log in again", "API key: managed key", ""])
def test_invalid_native_login_is_refused(payload):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=payload))
    with pytest.raises(native_login.LoginError, match="native Claude login is expired or missing"):
        native_login.check({}, "master", run)


@pytest.mark.parametrize("failure", [OSError("missing"), subprocess.TimeoutExpired("claude", 30)])
def test_unavailable_status_is_refused(failure):
    with pytest.raises(native_login.LoginError, match="native Claude login could not be checked"):
        native_login.check({}, "master", Mock(side_effect=failure))


def test_status_is_redacted_and_uses_native_environment(monkeypatch):
    monkeypatch.setattr(native_login.shutil, "which", lambda _: "/opt/claude")
    source = dict.fromkeys(["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"], "sentinel")
    source["HOME"] = "/native"
    payload = "Login method: Claude account\nEmail: private\nOrganization: private"
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=payload))
    proof = native_login.check(source, "master", run)
    assert proof == {"command": "claude auth status --text", "status": "Login method: Claude account"}
    run.assert_called_once_with(
        ["/opt/claude", "auth", "status", "--text"],
        env=envs.claude_interactive_child(source, "master"),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_first_successful_spawn_proof_is_kept(tmp_path):
    first = {"command": "claude auth status --text", "status": "Login method: Claude account"}
    native_login.record(tmp_path, "proof", first)
    native_login.record(tmp_path, "proof", {**first, "authMethod": "changed"})
    assert json.loads((tmp_path / "proof" / "interactive-claude-status.json").read_text()) == first
