"""UserPromptSubmit hands the operator's typed words to the gate lift, and only those."""

from unittest.mock import patch

import pytest

from hooks import hook_manager
from scripts.gates import lift
from scripts.gates.base import Who
from scripts.gates.entry import GATES

SWARM_ENV = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_AGENT_NAME": "engineer@1-1", "AGENTIHOOKS_SWARM_TASK": "t1"}


@pytest.fixture(autouse=True)
def swarm(monkeypatch):
    for key, value in SWARM_ENV.items():
        monkeypatch.setenv(key, value)
    with patch("hooks._redis.get_redis", return_value=None):
        yield


def test_arm_gate_lifts_passes_prompt_session_identity_and_known_gates():
    calls, logged = [], []
    with (
        patch.object(lift, "arm_from_prompt", lambda *a: calls.append(a) or ["identity"]),
        patch.object(hook_manager, "log", lambda *a: logged.append(a)),
    ):
        hook_manager._arm_gate_lifts({"prompt": "lift the identity gate", "session_id": "sid"})
    assert calls == [("lift the identity gate", "sid", Who.from_env(), GATES)]
    assert logged == [("gates: operator lift armed", {"session_id": "sid", "gates": ["identity"]})]


def test_arm_gate_lifts_logs_nothing_when_nothing_was_armed():
    logged = []
    with (
        patch.object(lift, "arm_from_prompt", lambda *a: []),
        patch.object(hook_manager, "log", lambda *a: logged.append(a)),
    ):
        hook_manager._arm_gate_lifts({"prompt": "hello", "session_id": "sid"})
    assert logged == []


def test_arm_gate_lifts_logs_a_failure_and_returns():
    def boom(*args):
        raise OSError("ledger down")

    logged = []
    with patch.object(lift, "arm_from_prompt", boom), patch.object(hook_manager, "log", lambda *a: logged.append(a)):
        hook_manager._arm_gate_lifts({"prompt": "lift the identity gate", "session_id": "sid"})
    assert logged == [("gate lift: arming or its ledger record failed", {"error": "ledger down"})]


@pytest.mark.parametrize("typed", [True, False])
def test_prompt_submit_arms_lifts_only_from_typed_words(typed):
    seen = []
    payload = {"session_id": "sid", "prompt": "lift the identity gate", "cwd": "/"}
    with (
        patch.object(hook_manager, "_operator_words", lambda p: typed),
        patch.object(hook_manager, "_arm_gate_lifts", seen.append),
    ):
        hook_manager.on_user_prompt_submit(payload)
    assert seen == ([payload] if typed else [])
