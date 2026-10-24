from types import SimpleNamespace

import pytest

from scripts.hive import account_slots


@pytest.mark.parametrize("route", ["subscription", "interactive", "master-login", "codex"])
def test_hive_admission_happens_after_the_route_resolves_and_before_exec(tmp_path, monkeypatch, route):
    from scripts import codex_router, install, master_claude_login
    from scripts.routing import interactive

    calls = []

    def admitted(harness, account, environ, invoke):
        assert account == "resolved"
        assert harness == ("codex" if route == "codex" else "claude")
        calls.append("slot")
        return invoke(dict(environ))

    monkeypatch.setattr(account_slots, "execute", admitted)
    monkeypatch.setattr(install.os, "execvpe", lambda *args: calls.append("exec"))
    monkeypatch.setattr(install, "_claude_command", lambda binary, args: [binary, *args])
    monkeypatch.setattr(master_claude_login, "prepare_launch", lambda *args: (tmp_path, "resolved"))
    monkeypatch.setattr(master_claude_login, "child", lambda *args: {})
    monkeypatch.setattr(interactive, "account", lambda *args: SimpleNamespace(slug="resolved"))
    if route == "master-login":
        master_claude_login.launch_claude("claude", [], {}, "")
    elif route == "interactive":
        interactive.launch_claude("claude", [], {}, "")
    elif route == "codex":
        monkeypatch.setattr(
            codex_router, "_route", lambda *args: (codex_router.CodexAccount("resolved"), "forced", 0, "1")
        )
        monkeypatch.setattr(codex_router, "credential", lambda account, env: account)
        monkeypatch.setattr(codex_router, "identity_warnings", lambda env: [])
        monkeypatch.setattr(codex_router, "command", lambda *args: ["codex"])
        monkeypatch.setattr(codex_router, "child_environment", lambda *args: {})
        monkeypatch.setattr("scripts.deps_preflight.refresh_codex_wrapper", lambda *args: None)
        codex_router.main([], {"HOME": str(tmp_path)}, lambda *args: calls.append("exec"))
    else:
        monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
        monkeypatch.setattr("scripts.deps_preflight.ensure", lambda: None)
        monkeypatch.setattr("scripts.claude_quota_balancer._cache_path", lambda env: tmp_path / "quota.json")
        monkeypatch.setattr("scripts.claude_quota_balancer.route_requires_fable", lambda *args: False)
        decision = SimpleNamespace(account="resolved")
        monkeypatch.setattr("scripts.claude_quota_balancer.forced", lambda *args: decision)
        monkeypatch.setattr("scripts.claude_quota_balancer.launch_environment", lambda *args: {})
        install.cmd_claude(["--route", "resolved"])
    assert calls == ["slot", "exec"]


def test_a_bare_claude_fallback_still_takes_the_unrouted_hive_slot(tmp_path, monkeypatch):
    from scripts import install
    from scripts.claude_quota_balancer import RoutingError

    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr("scripts.deps_preflight.ensure", lambda: None)
    monkeypatch.setattr("scripts.claude_quota_balancer._cache_path", lambda env: tmp_path / "quota.json")
    monkeypatch.setattr("scripts.claude_quota_balancer.route_requires_fable", lambda *args: False)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/opt/claude")

    def refused(*args, **kwargs):
        raise RoutingError("refused")

    def admitted(harness, account, env, invoke):
        assert (harness, account) == ("claude", "unrouted")
        return invoke({"KEPT": "yes"})

    def executed(binary, argv, env):
        assert binary == "/opt/claude"
        assert argv[0] == binary
        assert env == {"KEPT": "yes"}
        raise RuntimeError("exec reached")

    monkeypatch.setattr("scripts.claude_quota_balancer.select_credential", refused)
    monkeypatch.setattr(account_slots, "execute", admitted)
    monkeypatch.setattr(install.os, "execvpe", executed)
    with pytest.raises(RuntimeError, match="^exec reached$"):
        install.cmd_claude(["--agentihooks-fallback-bare"])
