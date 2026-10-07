import pytest

from scripts import agent_choice, herdr_host, init_agent


@pytest.fixture(autouse=True)
def _no_herdr(monkeypatch):
    monkeypatch.setattr(herdr_host, "binary", lambda: None)


def _quota(monkeypatch, **left):
    monkeypatch.setattr(agent_choice, "has_quota", lambda agent, environ: left.get(agent))
    monkeypatch.setattr(agent_choice, "at_cap", lambda agent, environ: False)


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
    monkeypatch.setattr("scripts.codex_quota.latest_codex_quota", lambda environ=None, keep=None: seen)
    assert agent_choice.has_quota("codex", {}) is True
    seen = CodexQuota(observed_at=0, plan_type="pro", seven_day=QuotaWindow(used=98.0))
    assert agent_choice.has_quota("codex", {}) is False


def test_a_codex_launcher_runs_through_the_codex_router(monkeypatch, tmp_path):
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: f"/usr/bin/{name}")
    env = {
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "rt"),
        "SHELL": "/bin/bash",
        "AGENTIHOOKS_TRUST_LAUNCH_DIR": "0",
    }
    launcher, prompt_file = init_agent._write_launcher(
        tmp_path, "eng-c", "fix it", ["--model", "o3"], env, init_agent.AgentSpec(agent="codex")
    )
    text = launcher.read_text()
    report = launcher.with_suffix(".route")
    assert (
        f"/usr/bin/agentihooks codex --agentihooks-report {report} -c 'model_reasoning_effort=\"high\"' --model o3 "
        f'"$(cat {prompt_file})"'
    ) in text
    assert "status=direct" not in text and "agentihooks claude" not in text


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
    assert "unsupported quota transfer" in capsys.readouterr().err


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


def test_one_claude_account_quota_comes_from_that_account_in_the_router_cache(monkeypatch):
    from scripts.claude_quota_balancer import ProbeResult, QuotaWindow

    def result(account, margin):
        return ProbeResult(account, "allowed", "NORMAL", margin, QuotaWindow(), QuotaWindow())

    monkeypatch.setattr(
        "scripts.claude_quota_balancer.cached_observations", lambda: [(0, result("a", 2.0)), (0, result("b", 40.0))]
    )
    assert agent_choice.account_has_quota("claude", "a", {}) is False
    assert agent_choice.account_has_quota("claude", "b", {}) is True
    assert agent_choice.account_has_quota("claude", "c", {}) is None


def test_one_codex_account_quota_uses_the_handoff_threshold(monkeypatch):
    from scripts import codex_router
    from scripts.claude_quota_balancer import QuotaWindow
    from scripts.codex_quota import CodexQuota

    seen = CodexQuota(observed_at=0, plan_type="pro", seven_day=QuotaWindow(used=98.0))
    monkeypatch.setattr("scripts.codex_quota.latest_codex_quota", lambda environ=None, keep=None: seen)
    assert agent_choice.account_has_quota("codex", codex_router.CODEX_DEFAULT, {}) is False
    seen = CodexQuota(observed_at=0, plan_type="pro", seven_day=QuotaWindow(used=50.0))
    assert agent_choice.account_has_quota("codex", codex_router.CODEX_DEFAULT, {}) is True
    assert agent_choice.account_has_quota("codex", "nobody", {}) is None


def test_an_agent_at_its_session_cap_is_skipped(monkeypatch):
    _quota(monkeypatch, claude=True, codex=True)
    monkeypatch.setattr(agent_choice, "at_cap", lambda agent, environ: agent == "claude")
    assert agent_choice.choose("", {}) == ("codex", "fallthrough: claude is at its session cap")


def test_every_agent_at_cap_is_reported(monkeypatch):
    _quota(monkeypatch, claude=True, codex=True)
    monkeypatch.setattr(agent_choice, "at_cap", lambda agent, environ: True)
    assert agent_choice.choose("", {})[1] == agent_choice.ALL_FULL


def test_codex_cap_counts_live_codex_sessions(monkeypatch):
    from hooks.context import account_sessions

    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {"default": 3})
    assert agent_choice.at_cap("codex", {}) is True
    assert agent_choice.at_cap("codex", {"AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT": "4"}) is False


def test_codex_is_full_only_when_every_codex_account_is(monkeypatch):
    from hooks.context import account_sessions
    from scripts import codex_router

    monkeypatch.setattr(codex_router, "default_signed_in", lambda environ, run=None: True)
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {"default": 3, "alpha": 3})
    assert agent_choice.at_cap("codex", {"AH_CX_TOKEN_alpha": "cx-a"}) is True
    assert agent_choice.at_cap("codex", {"AH_CX_TOKEN_alpha": "cx-a", "AH_CX_TOKEN_beta": "cx-b"}) is False


def test_claude_is_full_only_when_every_routable_account_is(monkeypatch):
    from hooks.context import account_sessions
    from scripts import claude_quota_balancer as bal

    monkeypatch.setattr(bal, "cached_observations", lambda: [(0, "a"), (0, "b")])
    monkeypatch.setattr(bal, "is_routable", lambda r: True)
    monkeypatch.setattr(agent_choice, "_account", lambda r: r)
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {"a": 3, "b": 2})
    assert agent_choice.at_cap("claude", {}) is False
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {"a": 3, "b": 3})
    assert agent_choice.at_cap("claude", {}) is True


def test_a_fresh_token_account_keeps_codex_available_when_the_default_is_spent(monkeypatch):
    from scripts import codex_router
    from scripts.claude_quota_balancer import QuotaWindow
    from scripts.codex_quota import CodexQuota

    spent = CodexQuota(observed_at=0, plan_type="pro", seven_day=QuotaWindow(used=99.0))
    environ = {"AH_CX_TOKEN_alpha": "cx-a"}
    monkeypatch.setattr(codex_router, "default_signed_in", lambda environ, run=None: True)
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {"default": spent, "alpha": None})
    assert agent_choice.has_quota("codex", environ) is True
    pool = codex_router.routing_pool(environ)
    assert codex_router.select(pool, {"default": spent, "alpha": None}, {}, cap=3)[0].name == "alpha"


def _share(monkeypatch, week_left=50.0, codex_full=False):
    monkeypatch.setattr(agent_choice, "codex_week_left", lambda environ: week_left)
    monkeypatch.setattr(agent_choice, "at_cap", lambda agent, environ: codex_full and agent == "codex")
    monkeypatch.setattr(agent_choice, "has_quota", lambda agent, environ: True)


def test_codex_share_below_target_picks_codex(monkeypatch):
    _share(monkeypatch)
    agent, reason = agent_choice.choose_shared("", {}, {"claude": 8, "codex": 2}, share=30, min_week_left=5)
    assert agent == "codex"
    assert "share" in reason


def test_codex_share_at_target_picks_claude(monkeypatch):
    _share(monkeypatch)
    assert agent_choice.choose_shared("", {}, {"claude": 7, "codex": 3}, share=30, min_week_left=5) == (
        "claude",
        "priority",
    )


def test_codex_week_left_under_the_minimum_picks_claude(monkeypatch):
    _share(monkeypatch, week_left=4.0)
    assert agent_choice.choose_shared("", {}, {}, share=30, min_week_left=5) == ("claude", "priority")


def test_unknown_codex_week_left_keeps_the_priority_choice(monkeypatch):
    _share(monkeypatch, week_left=None)
    assert agent_choice.choose_shared("", {}, {}, share=30, min_week_left=5) == ("claude", "priority")


def test_a_full_codex_keeps_the_priority_choice(monkeypatch):
    _share(monkeypatch, codex_full=True)
    assert agent_choice.choose_shared("", {}, {}, share=30, min_week_left=5) == ("claude", "priority")


def test_the_first_spawn_of_a_swarm_goes_to_codex(monkeypatch):
    _share(monkeypatch)
    assert agent_choice.choose_shared("", {}, {}, share=30, min_week_left=5)[0] == "codex"


def test_a_zero_share_never_picks_codex(monkeypatch):
    _share(monkeypatch)
    assert agent_choice.choose_shared("", {}, {}, share=0, min_week_left=5)[0] == "claude"


def test_an_explicit_lane_harness_wins_over_the_share(monkeypatch):
    _share(monkeypatch)
    assert agent_choice.choose_shared("claude", {}, {}, share=30, min_week_left=5) == ("claude", "requested")
    assert agent_choice.choose_shared("codex", {}, {"codex": 9}, share=30, min_week_left=5) == ("codex", "requested")


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("codex share 2/10 below 30%", "share"),
        ("priority", "share"),
        ("fallthrough: claude is at its session cap", "overflow"),
        ("fallthrough: claude has no quota", "overflow"),
        ("requested", "forced"),
        ("no agent has quota", "other"),
    ],
)
def test_each_router_reason_names_its_choice_kind(reason, kind):
    assert agent_choice.choice_kind(reason) == kind


def test_codex_week_left_is_the_best_signed_in_account(monkeypatch):
    from scripts import codex_router
    from scripts.claude_quota_balancer import QuotaWindow
    from scripts.codex_quota import CodexQuota

    pool = [
        codex_router.CodexAccount("default"),
        codex_router.CodexAccount("b", "AH_CX_TOKEN_b"),
        codex_router.CodexAccount("c", "AH_CX_TOKEN_c", signed_in=False),
    ]
    seen = {
        "default": CodexQuota(observed_at=0, plan_type="pro", seven_day=QuotaWindow(used=90.0)),
        "b": CodexQuota(observed_at=0, plan_type="pro", seven_day=QuotaWindow(used=40.0)),
        "c": CodexQuota(observed_at=0, plan_type="pro", seven_day=QuotaWindow(used=1.0)),
    }
    monkeypatch.setattr(codex_router, "routing_pool", lambda environ: pool)
    monkeypatch.setattr(codex_router, "quotas", lambda accounts, environ: {a.name: seen[a.name] for a in accounts})
    assert agent_choice.codex_week_left({}) == 60.0


def test_share_picks_count_share_choices_from_since_with_the_latest_row_per_name():
    rows = [
        {"name": "a", "harness": "codex", "started_at": 100, "choice": "overflow"},
        {"name": "a", "harness": "codex", "started_at": 100, "choice": "share"},
        {"name": "b", "harness": "claude", "started_at": 99, "choice": "share"},
        {"name": "c", "harness": "claude", "started_at": 100, "choice": "share"},
        {"name": "d", "harness": "codex", "started_at": 200, "choice": "forced"},
    ]
    assert agent_choice.share_picks(rows, 100) == {"codex": 1, "claude": 1}
