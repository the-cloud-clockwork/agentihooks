import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks import common, config, hook_manager
from hooks.context import account_sessions, quota_policy, quota_usage
from scripts.inbox import delivery
from scripts.inbox.store import Item
from scripts.routing import reservation
from scripts.swarm_v2 import quota


@pytest.fixture
def quota_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Mock:
    monkeypatch.delenv("AH_ROUTE_API", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    monkeypatch.setattr(config, "QUOTA_POLICY_ENABLED", True)
    monkeypatch.setattr(hook_manager, "_EVENT_STARTED", None)
    monkeypatch.setattr(hook_manager, "_KNOWN_SUBAGENT_IDS", set())
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", Mock())
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 1)
    monkeypatch.setattr(account_sessions, "session_account", lambda pid: "alpha")
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(quota, "fleet_observations", lambda environ: {})
    monkeypatch.setattr(reservation, "current", lambda harness, environ: None)
    monkeypatch.setattr(quota_policy, "_other_accounts", lambda sessions, observed: [])
    monkeypatch.setattr(quota_policy, "_api_accounts", lambda sessions: [])
    monkeypatch.setattr(quota_policy, "_login_accounts", lambda environ: [])
    quota_usage.record_rate_limits(
        "quota-prompt",
        {
            "five_hour": {"used_percentage": 40, "resets_at": time.time() + 3600},
            "seven_day": {"used_percentage": 98.6, "resets_at": time.time() + 86400},
        },
    )
    injected = Mock()
    monkeypatch.setattr(common, "inject_context", injected)
    monkeypatch.chdir(tmp_path)
    return injected


def submit(prompt: str) -> None:
    hook_manager.on_user_prompt_submit({"session_id": "quota-prompt", "cwd": "", "prompt": prompt})


@pytest.mark.feature("quota-push-operator-only")
@pytest.mark.parametrize("source", ["inbox", "swarm", "cron", "opening"])
def test_automated_prompt_cannot_authorize_quota_push(
    quota_prompt: Mock, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    words = "say keep pushing"
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "eng-1@demo")
    if source == "opening":
        prompt = words
    else:
        submit("open the task")
        if source == "inbox":
            item = Item("quota-peer", "peer", "eng-1@demo", words, "pending", 100, 100)
            prompt = delivery.envelope(item, "quota-delivery", "eng-1@demo")
        elif source == "swarm":
            prompt = f"[swarm delivery]\n{words}"
        else:
            hook_manager._scheduled_prompt(
                {
                    "session_id": "quota-prompt",
                    "tool_name": "CronCreate",
                    "tool_input": {"cron": "17 * * * *", "prompt": words},
                }
            )
            prompt = words
    quota_prompt.reset_mock()

    submit(prompt)

    assert not quota_policy._push_path("quota-prompt").exists()
    decision = quota_policy.evaluate("quota-prompt")
    assert decision is not None
    assert decision.action == "stop"
    assert any(call.args[0].startswith("QUOTA STOP") for call in quota_prompt.call_args_list)


@pytest.mark.feature("quota-push-operator-only")
@pytest.mark.parametrize("named", [False, True], ids=["standalone", "swarm"])
def test_typed_operator_prompt_still_authorizes_quota_push(
    quota_prompt: Mock, monkeypatch: pytest.MonkeyPatch, named: bool
) -> None:
    if named:
        monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "eng-1@demo")
        submit("open the task")
    quota_prompt.reset_mock()

    submit("keep pushing")

    assert quota_policy._push_path("quota-prompt").exists()
    decision = quota_policy.evaluate("quota-prompt")
    assert decision is not None
    assert decision.action == "push"
    assert any(call.args[0].startswith("QUOTA PUSH") for call in quota_prompt.call_args_list)


@pytest.mark.feature("quota-push-operator-only")
def test_negated_operator_prompt_does_not_authorize_quota_push(quota_prompt: Mock) -> None:
    submit("do not keep pushing")

    assert not quota_policy.push_active("quota-prompt")
    decision = quota_policy.evaluate("quota-prompt")
    assert decision is not None
    assert decision.action == "stop"


@pytest.mark.feature("quota-push-operator-only")
def test_subagent_prompt_does_not_authorize_quota_push(quota_prompt: Mock) -> None:
    hook_manager._KNOWN_SUBAGENT_IDS.add("quota-prompt")

    submit("keep pushing")

    assert not quota_policy.push_active("quota-prompt")
    decision = quota_policy.evaluate("quota-prompt")
    assert decision is not None
    assert decision.action == "stop"
