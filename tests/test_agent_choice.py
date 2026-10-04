import pytest

from scripts import agent_choice, herdr_host, init_agent


@pytest.fixture(autouse=True)
def _no_herdr(monkeypatch):
    monkeypatch.setattr(herdr_host, "binary", lambda: None)


def _quota(monkeypatch, **left):
    monkeypatch.setattr(agent_choice, "has_quota", lambda agent, environ: left.get(agent))


def test_an_explicit_agent_never_falls_through(monkeypatch):
    _quota(monkeypatch, claude=False, codex=True)
    assert agent_choice.choose("claude", {}) == ("claude", "requested")


def test_the_first_agent_with_quota_wins(monkeypatch):
    _quota(monkeypatch, claude=True, codex=True)
    assert agent_choice.choose("", {}) == ("claude", "priority")


def test_an_agent_without_quota_falls_through_to_the_next(monkeypatch):
    _quota(monkeypatch, claude=False, codex=True)
    assert agent_choice.choose("", {}) == ("codex", "fallthrough: claude has no quota")


def test_the_priority_list_comes_from_the_environment(monkeypatch):
    _quota(monkeypatch, claude=True, codex=True)
    assert agent_choice.choose("", {"AGENTIHOOKS_AGENT_PRIORITY": "codex, claude"}) == ("codex", "priority")


def test_unknown_quota_does_not_block_an_agent(monkeypatch):
    _quota(monkeypatch, claude=None, codex=True)
    assert agent_choice.choose("", {}) == ("claude", "priority")


def test_when_no_agent_has_quota_the_first_is_used(monkeypatch):
    _quota(monkeypatch, claude=False, codex=False)
    assert agent_choice.choose("", {}) == ("claude", "no agent has quota")


def test_codex_has_quota_below_the_handoff_threshold(monkeypatch):
    from scripts.claude_quota_balancer import QuotaWindow
    from scripts.codex_quota import CodexQuota

    seen = CodexQuota(observed_at=0, plan_type="pro", seven_day=QuotaWindow(used=97.0))
    monkeypatch.setattr("scripts.codex_quota.latest_codex_quota", lambda environ=None: seen)
    assert agent_choice.has_quota("codex", {}) is True
    seen = CodexQuota(observed_at=0, plan_type="pro", seven_day=QuotaWindow(used=98.0))
    assert agent_choice.has_quota("codex", {}) is False


def test_a_codex_launcher_runs_codex_and_reports_a_direct_route(monkeypatch, tmp_path):
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: f"/usr/bin/{name}")
    env = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt"), "SHELL": "/bin/bash"}
    launcher, prompt_file = init_agent._write_launcher(
        tmp_path, "eng-c", "fix it", ["--model", "o3"], env, init_agent.AgentSpec(agent="codex")
    )
    text = launcher.read_text()
    assert f'/usr/bin/codex --model o3 "$(cat {prompt_file})"' in text
    assert "status=direct" in text and str(launcher.with_suffix(".route")) in text
    assert "agentihooks claude" not in text


def test_init_agent_reports_the_chosen_agent(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        agent_choice, "choose", lambda requested, environ: ("codex", "fallthrough: claude has no quota")
    )
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    rc = init_agent.main(
        ["--dir", str(tmp_path), "--dry-run"], {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt")}
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "agent=codex" in out and "agent_reason=fallthrough: claude has no quota" in out


def test_a_handoff_stays_on_claude(monkeypatch, tmp_path, capsys):
    rc = init_agent.main(
        ["--dir", str(tmp_path), "--agent", "codex", "--handoff", "--prompt", "doc", "--dry-run"],
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt")},
    )
    assert rc == 2
    assert "--handoff moves work to another Claude account" in capsys.readouterr().err


def test_an_explicit_codex_agent_is_used_even_when_claude_has_quota(monkeypatch):
    _quota(monkeypatch, claude=True, codex=False)
    assert agent_choice.choose("codex", {}) == ("codex", "requested")


def test_claude_quota_comes_from_routable_accounts_in_the_router_cache(monkeypatch):
    from scripts.claude_quota_balancer import ProbeResult, QuotaWindow

    def result(margin):
        return ProbeResult("a", "allowed", "NORMAL", margin, QuotaWindow(), QuotaWindow())

    monkeypatch.setattr("scripts.claude_quota_balancer.cached_observations", lambda: [(0, result(2.0))])
    assert agent_choice.has_quota("claude", {}) is False
    monkeypatch.setattr(
        "scripts.claude_quota_balancer.cached_observations", lambda: [(0, result(2.0)), (0, result(40.0))]
    )
    assert agent_choice.has_quota("claude", {}) is True
    monkeypatch.setattr("scripts.claude_quota_balancer.cached_observations", lambda: [])
    assert agent_choice.has_quota("claude", {}) is None
