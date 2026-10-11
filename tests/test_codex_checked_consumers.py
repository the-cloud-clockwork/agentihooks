import os

import pytest

from hooks.context import account_sessions, quota_policy
from scripts import codex_router
from scripts.claude_quota_balancer import QuotaWindow
from scripts.codex_quota import CodexQuota


@pytest.fixture
def default_login(monkeypatch):
    for name in os.environ:
        if name.startswith(codex_router.TOKEN_ENV):
            monkeypatch.delenv(name)
    monkeypatch.delenv("AH_ROUTE_API", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    status = {"signed_in": False}
    monkeypatch.setattr(codex_router, "default_signed_in", lambda environ, run=None: status["signed_in"])
    quota = CodexQuota(1000.0, "pro", QuotaWindow(used=10.0), QuotaWindow(used=20.0))
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {account.name: quota for account in pool})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {"default": 1})
    monkeypatch.setattr(account_sessions, "sessions_by_account", dict)
    monkeypatch.setattr(quota_policy.time, "time", lambda: 1000.0)
    return status


def test_quota_handoff_excludes_signed_out_default_and_keeps_signed_in_default(monkeypatch, default_login):
    monkeypatch.setattr(quota_policy, "_session_windows", lambda session: (100.0, 100.0, None, None))
    monkeypatch.setattr(quota_policy, "_api_accounts", lambda sessions: [])
    monkeypatch.setattr(quota_policy, "push_active", lambda session: False)
    monkeypatch.setattr(account_sessions, "session_account", lambda pid: "drained")
    decision = quota_policy.evaluate("session")
    assert decision.action == "stop"
    assert decision.target is None
    assert decision.others == ()
    default_login["signed_in"] = True
    decision = quota_policy.evaluate("session")
    assert decision.action == "handoff"
    assert decision.target.account == "default"
    assert [candidate.account for candidate in decision.others] == ["default"]
