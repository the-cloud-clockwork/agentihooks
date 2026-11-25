"""On Windows the hook skips its swarm, quota, operator and lifecycle steps instead of failing inside them."""

import importlib
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

STEPS = [
    ("_confirm_inbox", ("session",), "hooks.context.inbox_delivery"),
    ("_swarm_heartbeat", ("idle",), "hooks.context.swarm_heartbeat"),
    ("_swarm_outcome", ({},), "hooks.context.swarm_heartbeat"),
    ("_master_login_checkpoint", (), "scripts.master_claude_login"),
    ("_swarm_ledger", ({},), "hooks.context.swarm_ledger"),
    ("_operator_words", ({},), "hooks.context.operator_words"),
    ("_operator_mode", ({}, False), "hooks.context.operator_mode"),
    ("_operator_reminder", ({},), "hooks.context.operator_mode"),
    ("_refocus_blocks", ("session", "PreToolUse"), "hooks.context.swarm_refocus"),
    ("_prompt_quota_policy", ({}, "session", False), "hooks.context.quota_policy"),
    ("_request_trace_flush", ({}, "stop"), "hooks.observability.trace_flush"),
    ("_close_handed_off", ({},), "hooks.lifecycle.handoff_close"),
]


@pytest.mark.parametrize(("name", "args", "module"), STEPS)
def test_windows_skips_the_step_without_reaching_its_module(
    monkeypatch: pytest.MonkeyPatch, name: str, args: tuple[object, ...], module: str
) -> None:
    """The step returns its empty result and logs no failure, with its module unimportable."""
    from hooks import hook_manager

    logged: list[str] = []
    monkeypatch.setattr(hook_manager, "log", lambda message, *_rest, **_kw: logged.append(message))
    parent, _, child = module.rpartition(".")
    monkeypatch.delattr(importlib.import_module(parent), child, raising=False)
    monkeypatch.setitem(sys.modules, module, None)
    monkeypatch.setattr(sys, "platform", "win32")
    assert getattr(hook_manager, name)(*args) in (None, False, [])
    assert logged == []


def test_lifecycle_guard_is_off_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifecycle sweep manages worktrees and leases, which a Windows assistant does not have."""
    from hooks.lifecycle import guard

    monkeypatch.setenv("LIFECYCLE_GC_ENABLED", "true")
    monkeypatch.setattr(sys, "platform", "win32")
    assert guard.enabled() is False


def test_codex_rollout_link_is_skipped_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rollout link feeds the swarm's session records, which a Windows assistant does not keep."""
    from hooks import hook_manager

    link = Mock()
    monkeypatch.setattr("hooks.targets.is_codex", lambda: True)
    monkeypatch.setattr("scripts.codex_rollouts.link_current", link)
    monkeypatch.setattr(sys, "platform", "win32")
    hook_manager._link_codex_rollout("rollout.jsonl")
    link.assert_not_called()


def test_windows_pre_tool_use_skips_the_quota_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Windows runs no quota routing, so its pre-tool hook never asks the quota policy to block."""
    from hooks import config, hook_manager

    policy = Mock(return_value=("BLOCKED: quota", None))
    monkeypatch.setattr(config, "QUOTA_POLICY_ENABLED", True)
    monkeypatch.setattr("hooks.context.conditions.write_guard", Mock(return_value=None))
    monkeypatch.setattr("hooks.context.conditions.pre_effect", Mock(return_value=None))
    monkeypatch.setattr("hooks.context.quota_policy.pretool", policy)
    monkeypatch.setattr(sys, "platform", "win32")
    hook_manager.on_pre_tool_use({"session_id": "windows", "tool_name": "Read", "tool_input": {}, "cwd": str(tmp_path)})
    policy.assert_not_called()
