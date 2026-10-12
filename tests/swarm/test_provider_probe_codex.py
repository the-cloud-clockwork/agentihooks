import base64
import json
import shutil
import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import codex_router
from scripts.swarm import provider_probe

pytestmark = pytest.mark.unit


def _oauth():
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "account-alpha"}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


@pytest.mark.parametrize("binary", ["/probe-bin/codex", None])
@pytest.mark.parametrize("route", ["access", "oauth", "default", "api"])
def test_codex_probe_launch_authenticates_like_the_routed_pane(tmp_path, monkeypatch, route, binary):
    token = _oauth() if route == "oauth" else uuid.uuid4().hex
    env = {"PATH": "probe-bin", "AGENTIHOOKS_AGENT_NAME": "parent"}
    if route in ("access", "oauth"):
        env.update({"AH_CX_TOKEN_alpha": token, "AH_CX_TOKEN_beta": uuid.uuid4().hex})
        account = codex_router.CodexAccount("alpha", "AH_CX_TOKEN_alpha")
    elif route == "api":
        env.update({"CODEX_API_KEY": uuid.uuid4().hex, "CODEX_API_BASE_URL": "https://provider.invalid"})
        account = codex_router.api_account(env)
    else:
        account = codex_router.CodexAccount("default")
    account = codex_router.credential(account, env)

    def which(name):
        assert name == "codex"
        return binary

    monkeypatch.setattr(shutil, "which", which)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        child = kwargs["env"]
        expected_env = codex_router.child_environment(account, env)
        expected_env.pop("AGENTIHOOKS_AGENT_NAME")
        from hooks.context.account_sessions import INTERNAL_RUN

        expected_env.update({"AGENTIHOOKS_CLASSIFIER_CHILD": "1", INTERNAL_RUN: "1"})
        assert child == expected_env
        if route in ("access", "oauth"):
            assert child["CODEX_ACCESS_TOKEN"] == token
            assert "AH_CX_TOKEN_beta" not in child
        if route == "oauth":
            assert child["AGENTIHOOKS_CHATGPT_ACCOUNT_ID"] == "account-alpha"
        native = [
            "exec",
            "--model",
            "chosen-model",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--json",
            "-c",
            'model_reasoning_effort="low"',
            "-c",
            'developer_instructions="Reply with exactly OK."',
        ]
        assert args == codex_router.command(account, binary if binary else "codex", native)
        assert kwargs["input"] == "Reply with exactly OK."
        output = '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n{"type":"turn.completed"}'
        return subprocess.CompletedProcess(args, 0, output, "")

    with patch.object(provider_probe, "os", SimpleNamespace(environ=env)):
        result = provider_probe.check(provider_probe.Target("codex", account.name, "chosen-model"), tmp_path, run)
    assert result.ok is True
    assert len(calls) == 1


@pytest.mark.parametrize("route", ["unknown", "alpha", "api", "rejected"])
def test_codex_probe_without_a_usable_account_never_launches(tmp_path, route):
    def run(*args, **kwargs):
        pytest.fail("An unavailable account must not launch")

    env = {"AH_CX_TOKEN_alpha": "sk-" + uuid.uuid4().hex} if route == "rejected" else {}
    account = "alpha" if route == "rejected" else route
    with patch.object(provider_probe, "os", SimpleNamespace(environ=env)):
        result = provider_probe.check(provider_probe.Target("codex", account, "model"), tmp_path, run)
    assert result.ok is False
    assert result.evidence == "Provider account route unavailable"
