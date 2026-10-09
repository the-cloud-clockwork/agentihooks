"""Tests for hooks.context.quota_policy — deterministic handoff / wait / stop."""

import time
from unittest.mock import patch

import pytest

from hooks.context import quota_policy as qp


def _c(account, five, week, sessions=0, cap=2):
    return qp.Candidate(account, five, week, sessions, time.time(), cap=cap)


def _decide(five, week, others, push=False):
    return qp.decide(
        account="alpha",
        five_used=five,
        week_used=week,
        five_reset=time.time() + 3600,
        week_reset=time.time() + 86400,
        others=others,
        push=push,
        five_pct=99,
        week_pct=98,
        min_left=20,
        wait_min_week_left=10,
    )


def test_below_both_triggers_nothing_happens():
    assert _decide(98.9, 97.9, []) is None


@pytest.mark.parametrize(
    ("five", "week", "others", "action", "target"),
    [
        # Weekly limit: move to the best account with room.
        (10, 98.5, [_c("beta", 10, 40)], "handoff", "beta"),
        # Weekly limit, every other account nearly spent: the least-used one still wins.
        (10, 98.5, [_c("beta", 5, 90), _c("gamma", 5, 93)], "handoff", "beta"),
        # Weekly limit, nothing else: stop.
        (10, 98.5, [], "stop", None),
        # 5h limit, other account has 5h free but its week 90% used: waiting here is better.
        (99.2, 50, [_c("beta", 0, 90)], "wait", None),
        # 5h limit, other account healthy: hand off.
        (99.2, 50, [_c("beta", 0, 30)], "handoff", "beta"),
        # 5h limit, only one account, week has room: wait for the reset.
        (99.2, 50, [], "wait", None),
        # 5h limit, week nearly spent too: the least-used other account wins.
        (99.2, 95, [_c("beta", 0, 90)], "handoff", "beta"),
        # 5h limit, week nearly spent, nothing else: stop.
        (99.2, 95, [], "stop", None),
        # Accounts at their own trigger are never targets.
        (10, 98.5, [_c("beta", 99.5, 10), _c("gamma", 10, 98.1)], "stop", None),
        # This session's own account in the cache is ignored.
        (10, 98.5, [_c("alpha", 0, 0)], "stop", None),
    ],
)
def test_decision_matrix(five, week, others, action, target):
    d = _decide(five, week, others)

    assert d.action == action
    assert (d.target.account if d.target else None) == target


def test_accounts_below_the_session_cap_are_preferred():
    d = _decide(10, 98.5, [_c("beta", 10, 20, sessions=2), _c("gamma", 10, 60, sessions=0)])

    assert d.target.account == "gamma"


def test_handoff_targets_are_judged_by_their_band_caps_alone():
    beta = qp.Candidate("beta", 10, 20, 2, time.time(), cap=4)
    gamma = qp.Candidate("gamma", 10, 60, 1, time.time(), cap=1)
    d = _decide(10, 98.5, [beta, gamma])

    assert d.target.account == "beta"
    assert "2/4 sessions" in qp._others_text(d)
    assert "1/1 sessions" in qp._others_text(d)


def test_other_accounts_carry_their_band_cap(monkeypatch):
    from scripts import claude_quota_balancer as balancer

    def result(account, five_used, week_used):
        return balancer.ProbeResult(
            account,
            "allowed",
            "NORMAL",
            70.0,
            balancer.QuotaWindow(five_used, None),
            balancer.QuotaWindow(week_used, None),
        )

    seen = [
        result("beta", 10.0, 30.0),
        result("gamma", 55.0, 30.0),
        result("delta", 10.0, 96.0),
        result("eps", 60.5, 30.0),
    ]
    monkeypatch.setattr(balancer, "cached_observations", lambda: [(time.time(), found) for found in seen])
    assert [(c.account, c.cap) for c in qp._other_accounts({"beta": 3})] == [
        ("beta", 6),
        ("gamma", 4),
        ("delta", 0),
        ("eps", 3),
    ]


@pytest.mark.parametrize("cap", [0, 2, 3, 4, 6])
def test_only_the_band_cap_decides_whether_an_account_is_full(cap):
    assert qp.Candidate("beta", 10, 30, cap, time.time(), cap=cap).full()
    assert qp.Candidate("beta", 10, 30, cap + 1, time.time(), cap=cap).full()
    if cap:
        assert not qp.Candidate("beta", 10, 30, cap - 1, time.time(), cap=cap).full()


def test_a_stale_target_has_no_session_cap_until_the_router_refreshes_it():
    stale = qp.Candidate("beta", 10, 30, 100, time.time(), cap=None)
    assert not stale.full()
    d = _decide(10, 98.5, [stale])
    assert d.target == stale


def test_other_accounts_count_an_account_with_no_live_session_as_zero(monkeypatch):
    from scripts import claude_quota_balancer as balancer

    beta = balancer.ProbeResult(
        "beta", "allowed", "NORMAL", 70.0, balancer.QuotaWindow(10.0, None), balancer.QuotaWindow(30.0, None)
    )
    observed = time.time()
    monkeypatch.setattr(balancer, "cached_observations", lambda: [(observed, beta), (observed - 901, beta)])
    [found] = qp._other_accounts({})
    assert (found.sessions, found.observed_at) == (0, observed)


def test_a_stale_reading_stays_a_target_with_no_band_cap_for_the_router_to_refresh(monkeypatch):
    from scripts import claude_quota_balancer as balancer

    beta = balancer.ProbeResult(
        "beta", "allowed", "NORMAL", 70.0, balancer.QuotaWindow(10.0, None), balancer.QuotaWindow(30.0, None)
    )
    observed = time.time() - 901
    monkeypatch.setattr(balancer, "cached_observations", lambda: [(observed, beta)])
    [found] = qp._other_accounts({})
    assert (found.account, found.observed_at, found.cap) == ("beta", observed, None)


def test_an_open_account_with_exactly_the_minimum_routing_left_wins_over_a_full_one():
    edge = qp.Candidate("beta", 0, 100 - qp.MIN_ROUTING_LEFT, 0, time.time(), cap=6)
    full = qp.Candidate("gamma", 10, 50, 5, time.time(), cap=4)
    assert _decide(10, 98.5, [edge, full]).target.account == "beta"


def test_among_open_good_accounts_the_most_routing_left_wins():
    assert _decide(10, 98.5, [_c("beta", 10, 70), _c("gamma", 10, 30)]).target.account == "gamma"


def test_a_stale_target_shows_an_unknown_cap_in_the_texts():
    stale = qp.Candidate("beta", 10, 40, 1, time.time(), cap=None)
    d = _decide(10, 98.5, [stale])
    assert qp._others_text(d) == "beta 60% left (5h 10%, 7d 40% used, 1/unknown sessions)"
    assert "1/unknown sessions" in qp.render(d, "sess-1", "/tmp")


def test_operator_push_replaces_stop_and_wait():
    assert _decide(10, 98.5, [], push=True).action == "push"
    assert _decide(99.2, 50, [], push=True).action == "push"
    assert _decide(10, 98.5, [_c("beta", 10, 40)], push=True).action == "handoff"


def test_push_signal_is_recorded_only_when_not_negated(tmp_path):
    with patch.object(qp, "AGENTIHOOKS_HOME", tmp_path):
        assert not qp.record_push_signal("s1", "don't keep pushing")
        assert not qp.push_active("s1")
        assert qp.record_push_signal("s1", "ok keep pushing until it dies")
        assert qp.push_active("s1")
        qp.clear_session_state("s1")
        assert not qp.push_active("s1")


def test_handoff_directive_names_the_command_and_the_document():
    d = _decide(10, 98.5, [_c("beta", 10, 40)])

    text = qp.render(d, "sess-1", "/home/u/dev/repo")

    assert text.startswith("QUOTA HANDOFF REQUIRED")
    assert "agentihooks init-agent --handoff" in text
    assert "sess-1.md" in text
    assert "beta has 60% left" in text


def test_wait_directive_carries_a_cron_for_the_reset():
    d = _decide(99.2, 50, [])

    text = qp.render(d, "sess-1", "/tmp")

    assert text.startswith("QUOTA WAIT")
    assert 'cron "' in text
    assert "CronCreate" in text


def test_stop_directive_asks_for_another_account():
    text = qp.render(_decide(10, 98.5, []), "sess-1", "/tmp")

    assert text.startswith("QUOTA STOP")
    assert "at least 2 accounts" in text
    assert '"keep pushing"' in text


@pytest.mark.parametrize(
    ("five", "week", "tool", "blocked", "context"),
    [
        (10, 98.5, "Bash", True, False),
        (99.2, 50, "Bash", True, False),
        (99.2, 50, "CronCreate", False, True),
    ],
)
def test_pretool_blocks_stop_and_wait(five, week, tool, blocked, context):
    d = _decide(five, week, [])
    with patch.object(qp, "evaluate", return_value=d), patch.object(qp, "handed_off_block", return_value=None):
        block, ctx = qp.pretool("sess-1", tool, "/tmp")

    assert bool(block) is blocked
    assert bool(ctx) is context


def test_pretool_injects_handoff_without_blocking():
    d = _decide(10, 98.5, [_c("beta", 10, 40)])
    with patch.object(qp, "evaluate", return_value=d), patch.object(qp, "handed_off_block", return_value=None):
        block, ctx = qp.pretool("sess-1", "Write", "/tmp")

    assert block is None
    assert ctx.startswith("QUOTA HANDOFF REQUIRED")


def test_handed_off_session_is_blocked():
    with patch(
        "hooks.context.broadcast.session_status", return_value={"status": "handed_off", "handed_off_to": "beta"}
    ):
        block, ctx = qp.pretool("sess-1", "Bash", "/tmp")

    assert "handed its task off to account beta" in block
    assert ctx is None


def test_spent_windows_reset_after_their_reset_time():
    assert qp._effective(99.0, time.time() - 1, time.time()) == 0.0
    assert qp._effective(99.0, time.time() + 60, time.time()) == 99.0


def test_evaluate_reads_the_session_snapshot_and_the_router_cache(tmp_path, monkeypatch):
    from hooks.context import quota_usage
    from scripts import claude_quota_balancer as balancer

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setattr(quota_usage, "AGENTIHOOKS_HOME", tmp_path)
    now = time.time()
    quota_usage.record_rate_limits(
        "sess-1",
        {
            "five_hour": {"used_percentage": 40, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 98.6, "resets_at": now + 86400},
        },
    )
    beta = balancer.ProbeResult(
        "beta",
        "allowed",
        "NORMAL",
        70.0,
        balancer.QuotaWindow(10.0, int(now + 3600)),
        balancer.QuotaWindow(30.0, int(now + 86400)),
    )
    balancer._write_cache(
        tmp_path / "claude-router-cache.json",
        {
            "version": 1,
            "modes": {
                "normal": {
                    "accounts": {"AH_CC_TOKEN_beta": {"observed_at": now, "result": balancer._result_data(beta)}}
                }
            },
        },
    )
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda start=None: 1)
    monkeypatch.setattr("hooks.context.account_sessions.session_account", lambda pid: "alpha")
    monkeypatch.setattr("hooks.context.account_sessions.sessions_by_account", lambda: {"alpha": 1, "beta": 1})

    d = qp.evaluate("sess-1")
    assert d.target.cap == 6

    assert d.action == "handoff"
    assert d.trigger == "week"
    assert d.target.account == "beta"
    assert d.target.sessions == 1


def test_pre_tool_use_raises_the_stop_block():
    from hooks import hook_manager

    d = _decide(10, 98.5, [])
    with patch.object(qp, "evaluate", return_value=d), patch.object(qp, "handed_off_block", return_value=None):
        with pytest.raises(hook_manager.BlockAction, match="QUOTA STOP"):
            hook_manager.on_pre_tool_use(
                {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}, "session_id": "sess-stop", "cwd": "/tmp"}
            )


def test_reserve_account_is_the_last_handoff_target():
    others = [_c("big", 0, 30), _c("small", 0, 88), _c("tiny", 0, 96)]

    def pick(reserve):
        return qp.decide(
            account="alpha",
            five_used=10,
            week_used=97.5,
            five_reset=None,
            week_reset=None,
            others=others,
            push=False,
            week_pct=97,
            min_left=20,
            reserve=reserve,
        )

    assert pick(frozenset()).target.account == "big"
    assert pick(frozenset({"big"})).target.account == "small"
    assert (
        qp.decide(
            account="alpha",
            five_used=10,
            week_used=97.5,
            five_reset=None,
            week_reset=None,
            others=[_c("big", 0, 30), _c("tiny", 0, 96)],
            push=False,
            week_pct=97,
            min_left=20,
            reserve=frozenset({"big"}),
        ).target.account
        == "big"
    )


def test_reserve_account_below_the_cap_takes_the_handoff_before_full_accounts():
    def pick(others):
        return qp.decide(
            account="alpha",
            five_used=10,
            week_used=97.5,
            five_reset=None,
            week_reset=None,
            others=others,
            push=False,
            week_pct=97,
            min_left=20,
            reserve=frozenset({"spare"}),
        ).target.account

    assert pick([_c("big", 0, 12, 8), _c("mid", 0, 44, 7), _c("spare", 0, 62, 0)]) == "spare"
    assert pick([_c("big", 0, 12, 8), _c("mid", 0, 44, 2, cap=3), _c("spare", 0, 62, 0)]) == "mid"


@pytest.mark.parametrize("five,week", [(99.2, 50), (10, 98.5), (100, 100)])
def test_api_account_has_no_subscription_quota_decision(five, week):
    assert (
        qp.decide(
            account="api",
            five_used=five,
            week_used=week,
            five_reset=time.time() + 3600,
            week_reset=time.time() + 86400,
            others=[],
            push=False,
        )
        is None
    )


@pytest.mark.parametrize("tool", ["Bash", "CronCreate"])
def test_api_route_bypasses_snapshot_and_subscription_policy(monkeypatch, tool):
    monkeypatch.setenv("AH_ROUTE_API", "1")
    monkeypatch.setattr(qp, "_session_windows", lambda session: pytest.fail("api read subscription windows"))
    assert qp.evaluate("api-session") is None
    assert qp.pretool("api-session", tool, "/repo") == (None, None)
    assert qp.prompt_context("api-session", "/repo") is None


@pytest.mark.parametrize("five,week", [(99.2, 50), (10, 98.5), (99.2, 95)])
@pytest.mark.parametrize(
    "sessions,cap,expected", [(0, 1, "handoff"), (1, 1, None), (0, 0, None), (100, None, "handoff")]
)
def test_api_slot_replaces_stop_and_wait_only_with_room(five, week, sessions, cap, expected):
    api = qp.Candidate("api", None, None, sessions, time.time(), cap)
    baseline = _decide(five, week, [])
    decision = _decide(five, week, [api])
    assert decision.action == (expected or baseline.action)
    assert decision.trigger == baseline.trigger
    assert (decision.target.account if decision.target else None) == ("api" if expected else None)
    text = qp.render(decision, "session", "/repo")
    assert "--route api" in text if expected else "--route api" not in text
    if expected:
        import shlex

        from scripts.init_agent import _parser

        command = next(line.removeprefix("2. Run: ") for line in text.splitlines() if line.startswith("2. Run: "))
        parsed = _parser().parse_args(shlex.split(command)[2:])
        assert parsed.handoff
        assert parsed.claude_args == ["--", "--route", "api"]


def test_api_fallback_preserves_subscription_target_and_operator_push():
    api = qp.Candidate("api", None, None, 0, time.time(), None)
    beta = _c("beta", 0, 30)
    assert _decide(99.2, 50, [api, beta]).target == beta
    assert _decide(99.2, 50, [api], push=True).action == "push"
    assert _decide(10, 98.5, [api], push=True).action == "push"
    assert _decide(0, 0, [api]) is None


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize(
    "available,live,cap,expected", [(False, 0, 1, "wait"), (True, 0, 1, "handoff"), (True, 1, 1, "wait")]
)
def test_evaluate_detects_api_for_its_harness(monkeypatch, harness, available, live, cap, expected):
    from scripts.routing import claude_api, codex_api, place

    monkeypatch.delenv("AH_ROUTE_API", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_TARGET", harness)
    monkeypatch.setattr(qp, "_session_windows", lambda session: (100, 50, time.time() + 3600, None))
    monkeypatch.setattr("scripts.claude_quota_balancer.cached_observations", lambda: [])
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda: 1)
    monkeypatch.setattr("hooks.context.account_sessions.session_account", lambda pid: "alpha")
    monkeypatch.setattr(
        "hooks.context.account_sessions.sessions_by_account", lambda: {"api": live if harness == "claude" else 100}
    )
    monkeypatch.setattr("hooks.context.account_sessions.codex_sessions_by_account", lambda: {"api": live})
    monkeypatch.setattr(claude_api, "provider", lambda env: "test" if available and harness == "claude" else "")
    monkeypatch.setattr(codex_api, "provider", lambda env: "test" if available and harness == "codex" else "")
    monkeypatch.setattr(place, "policy", lambda target, env: place.ApiPolicy(0, cap))
    monkeypatch.setattr(qp, "push_active", lambda session: False)
    decision = qp.evaluate("subscription")
    assert decision.action == expected
    assert (decision.target.account if decision.target else None) == ("api" if expected == "handoff" else None)
    if expected == "handoff":
        assert f"--agent {harness}" in qp.render(decision, "subscription", "/repo")


@pytest.mark.parametrize("five,week", [(80, 0), (0, 80)])
def test_configured_handoff_threshold_excludes_exactly_spent_targets(five, week):
    decision = qp.decide(
        account="alpha",
        five_used=100,
        week_used=100,
        five_reset=None,
        week_reset=None,
        others=[_c("beta", five, week)],
        push=False,
        five_pct=80,
        week_pct=80,
    )
    assert decision.action == "stop"


def test_subscription_handoff_command_preserves_the_launcher_arguments():
    import shlex

    from scripts.init_agent import _parser

    text = qp.render(_decide(10, 98.5, [_c("beta", 0, 0)]), "session", "/repo")
    command = next(line.removeprefix("2. Run: ") for line in text.splitlines() if line.startswith("2. Run: "))
    parsed = _parser().parse_args(shlex.split(command)[2:])
    assert parsed.handoff
    assert parsed.agent == ""
    assert parsed.claude_args == []


@pytest.mark.parametrize("harness", [None, "claude", "codex"])
def test_api_slot_uses_its_harness_settings_namespace(tmp_path, monkeypatch, harness):
    from scripts.routing import claude_api, codex_api, place
    from scripts.routing.settings import FileSettings

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    if harness is None:
        monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    else:
        monkeypatch.setenv("AGENTIHOOKS_TARGET", harness)
    monkeypatch.setattr(place, "_client", lambda env: None)
    monkeypatch.setattr(claude_api, "provider", lambda env: "test")
    monkeypatch.setattr(codex_api, "provider", lambda env: "test")
    monkeypatch.setattr("hooks.context.account_sessions.codex_sessions_by_account", lambda: {"api": 2})
    settings = FileSettings(tmp_path / "routing-settings.json")
    settings.set("claude-api-max-sessions", 3, "operator", time.time())
    settings.set("codex-api-max-sessions", 4, "operator", time.time())
    (candidate,) = qp._api_accounts({"api": 1})
    assert candidate.account == "api"
    assert candidate.sessions == (2 if harness == "codex" else 1)
    assert candidate.cap == (4 if harness == "codex" else 3)
    assert (candidate.five_used, candidate.week_used) == (None, None)
