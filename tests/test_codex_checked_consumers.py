import os

import pytest

from hooks.context import account_sessions, quota_policy
from scripts import codex_router
from scripts.claude_quota_balancer import QuotaWindow
from scripts.codex_quota import CodexQuota


@pytest.fixture
def default_login(monkeypatch):
    for name in os.environ:
        if name.startswith(codex_router.CODEX_TOKEN_PREFIX):
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
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {account.name: None for account in pool})
    decision = quota_policy.evaluate("session")
    assert decision.action == "stop"
    assert decision.others == ()


@pytest.mark.parametrize("surface", ["page", "table"])
def test_quota_rows_exclude_signed_out_default_and_keep_signed_in_default(monkeypatch, default_login, surface):
    from scripts import agents_quota, claude_quota_balancer

    monkeypatch.setattr(claude_quota_balancer, "cached_observations", list)
    monkeypatch.setattr(agents_quota, "_masters", lambda *args: {})
    monkeypatch.setattr(agents_quota, "api_rows", lambda *args: [])

    def rows():
        if surface == "table":
            return agents_quota.codex_table()
        agents_quota._page_cache.clear()
        return [row["account"] for row in agents_quota.page_quota(now=1000.0)["rows"]]

    assert "default" not in rows()
    default_login["signed_in"] = True
    assert "default" in rows()


def test_agent_choice_quota_excludes_signed_out_default_and_keeps_signed_in_default(monkeypatch, default_login):
    from scripts import agent_choice
    from scripts.swarm import capacity

    monkeypatch.setattr(capacity, "_claude", lambda environ, now: [])
    monkeypatch.setattr(capacity, "_api", lambda *args: [])
    assert agent_choice.account_has_quota("codex", "default", dict(os.environ)) is None
    assert agent_choice.choose("", dict(os.environ)) == ("", agent_choice.ALL_FULL)
    default_login["signed_in"] = True
    assert agent_choice.account_has_quota("codex", "default", dict(os.environ)) is True
    assert agent_choice.choose("", dict(os.environ)) == ("codex", "rotation")
