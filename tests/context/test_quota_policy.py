"""Tests for hooks.context.quota_policy — deterministic handoff / wait / stop."""

import time
from unittest.mock import patch

import pytest

from hooks.context import quota_policy as qp
from scripts.routing.reservation import Reservation


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
    for account in ("beta", "gamma", "delta", "eps"):
        monkeypatch.setenv(f"AH_CC_TOKEN_{account}", account)
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
    monkeypatch.setenv("AH_CC_TOKEN_beta", "available")
    from scripts import claude_quota_balancer as balancer

    beta = balancer.ProbeResult(
        "beta", "allowed", "NORMAL", 70.0, balancer.QuotaWindow(10.0, None), balancer.QuotaWindow(30.0, None)
    )
    observed = time.time()
    monkeypatch.setattr(balancer, "cached_observations", lambda: [(observed, beta), (observed - 901, beta)])
    [found] = qp._other_accounts({})
    assert (found.sessions, found.observed_at) == (0, observed)


def test_a_stale_reading_stays_a_target_with_no_band_cap_for_the_router_to_refresh(monkeypatch):
    monkeypatch.setenv("AH_CC_TOKEN_beta", "available")
    from scripts import claude_quota_balancer as balancer

    beta = balancer.ProbeResult(
        "beta", "allowed", "NORMAL", 70.0, balancer.QuotaWindow(10.0, None), balancer.QuotaWindow(30.0, None)
    )
    observed = time.time() - 901
    monkeypatch.setattr(balancer, "cached_observations", lambda: [(observed, beta)])
    [found] = qp._other_accounts({})
    assert (found.account, found.observed_at, found.cap) == ("beta", observed, None)


def test_a_newer_fleet_observation_outranks_the_local_cache(monkeypatch):
    monkeypatch.setenv("AH_CC_TOKEN_beta", "available")
    from scripts import claude_quota_balancer as balancer

    def beta(five_used):
        return balancer.ProbeResult(
            "beta", "allowed", "NORMAL", 70.0, balancer.QuotaWindow(five_used, None), balancer.QuotaWindow(30.0, None)
        )

    observed = time.time()
    monkeypatch.setattr(balancer, "cached_observations", lambda: [(observed - 60, beta(0.0))])
    [found] = qp._other_accounts({}, [(observed, beta(100.0))])
    assert (found.five_used, found.observed_at) == (100.0, observed)
    [kept] = qp._other_accounts({}, [(observed - 120, beta(100.0))])
    assert kept.five_used == 0.0


def test_a_fleet_observation_wins_a_tie_with_the_local_cache(monkeypatch):
    monkeypatch.setenv("AH_CC_TOKEN_beta", "available")
    from scripts import claude_quota_balancer as balancer

    def beta(five_used):
        return balancer.ProbeResult(
            "beta", "allowed", "NORMAL", 70.0, balancer.QuotaWindow(five_used, None), balancer.QuotaWindow(30.0, None)
        )

    observed = time.time()
    monkeypatch.setattr(balancer, "cached_observations", lambda: [(observed, beta(0.0))])
    [found] = qp._other_accounts({}, [(observed, beta(100.0))])
    assert found.five_used == 100.0


def test_evaluate_reads_fleet_observations_from_its_environment(monkeypatch):
    seen = []
    monkeypatch.delenv("AH_ROUTE_API", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_FLEET_QUOTA", "1")
    monkeypatch.setattr(qp, "_session_windows", lambda session: (100, 50, time.time() + 3600, None))
    monkeypatch.setattr("scripts.swarm_v2.quota.fleet_observations", lambda environ: seen.append(environ) or [])
    monkeypatch.setattr(qp, "_other_accounts", lambda sessions, fleet: [_c("beta", 10, 30)] if fleet == [] else [])
    monkeypatch.setattr(qp, "_api_accounts", lambda sessions: [])
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda: 1)
    monkeypatch.setattr("hooks.context.account_sessions.session_account", lambda pid: "alpha")
    monkeypatch.setattr("hooks.context.account_sessions.sessions_by_account", lambda: {})
    monkeypatch.setattr(qp, "push_active", lambda session: False)
    decision = qp.evaluate("subscription")
    assert (decision.action, decision.target.account) == ("handoff", "beta")
    assert seen[0]["AGENTIHOOKS_FLEET_QUOTA"] == "1"


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


def test_a_swarm_session_hands_off_through_its_seat(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")
    d = _decide(10, 98.5, [_c("beta", 10, 40)])

    text = qp.render(d, "sess-1", "/home/u/dev/repo")

    assert text.startswith("QUOTA HANDOFF REQUIRED")
    assert "Run: agentihooks swarm demo handoff DOC --reason quota\n" in text
    assert "init-agent" not in text
    assert "keeps your seat and task" in text


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
    ("five", "week", "tool", "head"),
    [
        (10, 98.5, "Bash", "QUOTA STOP"),
        (99.2, 50, "Bash", "QUOTA WAIT"),
        (99.2, 50, "CronCreate", "QUOTA WAIT"),
    ],
)
def test_pretool_alerts_stop_and_wait_without_blocking(five, week, tool, head):
    d = _decide(five, week, [])
    with patch.object(qp, "evaluate", return_value=d), patch.object(qp, "handed_off_block", return_value=None):
        block, ctx = qp.pretool("sess-1", tool, "/tmp")

    assert block is None
    assert ctx.startswith(head)
    assert "Tools are not blocked." in ctx


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
    monkeypatch.setenv("AH_CC_TOKEN_beta", "available")
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


@pytest.mark.parametrize("source", ["cache", "fleet"])
@pytest.mark.parametrize("credentials", ["missing", "empty", "environment", "operator"])
def test_handoff_ignores_cached_accounts_without_credentials(tmp_path, monkeypatch, source, credentials):
    from dataclasses import replace

    from hooks.context import account_sessions, quota_usage
    from scripts import claude_quota_balancer as balancer
    from scripts.swarm_v2 import quota

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv("AH_ROUTE_API", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    for name in tuple(qp.os.environ):
        if name.startswith(balancer.TOKEN_PREFIX):
            monkeypatch.delenv(name)
    available = credentials in ("environment", "operator")
    if credentials == "environment":
        monkeypatch.setenv("AH_CC_TOKEN_beta", "available")
    elif credentials == "empty":
        monkeypatch.setenv("AH_CC_TOKEN_beta", "")
    elif credentials == "operator":
        monkeypatch.setattr("scripts.operator_env.block_tokens", lambda: {"AH_CC_TOKEN_beta": "available"})
    monkeypatch.setattr(quota_usage, "AGENTIHOOKS_HOME", tmp_path)
    now = time.time()
    quota_usage.record_rate_limits(
        "stale-target",
        {
            "five_hour": {"used_percentage": 40, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 99, "resets_at": now + 86400},
        },
    )
    beta = balancer.ProbeResult(
        "beta", "allowed", "NORMAL", 70.0, balancer.QuotaWindow(10.0, None), balancer.QuotaWindow(30.0, None)
    )
    balancer._write_cache(
        tmp_path / "claude-router-cache.json",
        {
            "version": 1,
            "modes": {
                "normal": {
                    "accounts": {
                        "AH_CC_TOKEN_removed": {
                            "observed_at": now,
                            "result": balancer._result_data(replace(beta, account="removed")),
                        },
                        "AH_CC_TOKEN_beta": {"observed_at": now, "result": balancer._result_data(beta)},
                    }
                }
            },
        }
        if source == "cache"
        else {},
    )
    monkeypatch.setattr(
        quota,
        "fleet_observations",
        lambda environ: [(now, replace(beta, account="removed")), (now, beta)] if source == "fleet" else [],
    )
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 1)
    monkeypatch.setattr(account_sessions, "session_account", lambda pid: "alpha")
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda **_: {"alpha": 1, "beta": 1})
    monkeypatch.setattr(qp, "_api_accounts", lambda sessions: [])
    monkeypatch.setattr(qp, "push_active", lambda session: False)

    decision = qp.evaluate("stale-target")
    assert decision.action == ("handoff" if available else "stop")
    assert (decision.target.account if decision.target else None) == ("beta" if available else None)


def test_pre_tool_use_lets_the_tool_run_under_the_stop_alert():
    from hooks import hook_manager

    d = _decide(10, 98.5, [])
    with patch.object(qp, "evaluate", return_value=d), patch.object(qp, "handed_off_block", return_value=None):
        hook_manager.on_pre_tool_use(
            {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}, "session_id": "sess-stop", "cwd": "/tmp"}
        )


def test_pre_tool_use_raises_the_handed_off_block(monkeypatch):
    from hooks import hook_manager

    monkeypatch.setattr("hooks.config.QUOTA_POLICY_ENABLED", True)
    reason = "BLOCKED: this session handed its task off to account beta at 09:00."
    with (
        patch.object(qp, "handed_off_block", return_value=reason),
        patch.object(qp, "evaluate") as evaluate,
    ):
        with pytest.raises(hook_manager.BlockAction, match="handed its task off to account beta"):
            hook_manager.on_pre_tool_use(
                {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}, "session_id": "sess-moved", "cwd": "/tmp"}
            )
    evaluate.assert_not_called()


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
    monkeypatch.setattr(qp, "_codex_accounts", lambda: [])
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


@pytest.mark.parametrize(
    ("pid", "session", "blocked"), [(4242, "thread", True), (4243, "thread", False), (4242, "", False)]
)
def test_a_process_that_handed_its_resumed_conversation_on_is_blocked(tmp_path, monkeypatch, pid, session, blocked):
    from hooks.lifecycle import handoff_close

    monkeypatch.setattr(handoff_close, "_home", lambda: tmp_path)
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda: pid)
    monkeypatch.setattr("hooks.context.broadcast.session_status", lambda session_id: {"status": "alive"})
    handoff_close.handoff_record(4242, "thread").touch()
    handoff_close.handoff_record(4242, "").touch()
    expected = "BLOCKED: this session handed its task off to account ? at ?. Stop working here and tell the operator where the work continues."
    assert qp.handed_off_block(session) == (expected if blocked else None)


def test_a_prompt_to_a_handed_off_process_gets_its_block_instead_of_a_new_handoff(monkeypatch):
    def handoff(session_id):
        raise AssertionError("a handed off process must not evaluate a new handoff")

    monkeypatch.setattr(qp, "handed_off_block", lambda session_id: f"BLOCKED: {session_id} moved")
    monkeypatch.setattr(qp, "evaluate", handoff)
    assert qp.prompt_context("thread", "/repo") == "BLOCKED: thread moved"


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


MASTER_HELD = Reservation("claude", "main", 1)


def _decide_reserved(week_used, ceiling, others=()):
    return qp.decide_reserved(
        ceiling,
        account="main",
        five_used=10.0,
        week_used=week_used,
        five_reset=None,
        week_reset=None,
        others=list(others),
        push=False,
    )


def test_worker_on_the_master_account_hands_off_at_the_ceiling():
    other = qp.Candidate("spare", 10.0, 10.0, 0, 0.0, 6)
    assert _decide_reserved(69.9, 70.0, [other]) is None
    decision = _decide_reserved(70.0, 70.0, [other])
    assert (decision.action, decision.trigger, decision.target.account) == ("handoff", "week", "spare")
    assert decision.week_used == 70.0
    assert _decide_reserved(70.0, None, [other]) is None
    assert _decide_reserved(98.5, None, [other]).week_used == 98.5


def test_reserved_candidates_take_no_handoff():
    held = qp.reserved([qp.Candidate("main", 10.0, 70.0, 0, 0.0, 6)], MASTER_HELD)
    assert held[0].reserved
    assert not qp.reserved([qp.Candidate("main", 10.0, 69.9, 0, 0.0, 6)], MASTER_HELD)[0].reserved
    assert qp.reserved(held, None) == held
    decision = qp.decide(
        account="tired",
        five_used=10.0,
        week_used=99.0,
        five_reset=None,
        week_reset=None,
        others=held,
        push=False,
    )
    assert decision.action == "stop"


def test_a_master_session_ignores_the_ceiling():
    assert qp._own_ceiling(MASTER_HELD, "main", master=True) is None
    assert qp._own_ceiling(MASTER_HELD, "main", master=False) == 70.0
    assert qp._own_ceiling(MASTER_HELD, "other", master=False) is None
    assert qp._own_ceiling(None, "main", master=False) is None


def test_a_ceiling_handoff_names_the_ceiling_as_its_limit():
    spare = qp.Candidate("spare", 10.0, 10.0, 0, 0.0, 6)
    assert qp._trigger_text(_decide_reserved(70.0, 70.0, [spare])) == "7-day quota 70.0% used (limit 70%)"
    assert _decide_reserved(98.5, None, [spare]).week_limit == qp.QUOTA_HANDOFF_WEEK_PCT


def test_a_spent_five_hour_window_at_the_ceiling_stops_on_the_week():
    decision = qp.decide_reserved(
        70.0,
        account="main",
        five_used=99.5,
        week_used=75.0,
        five_reset=1.0,
        week_reset=None,
        others=[],
        push=False,
    )
    assert (decision.action, decision.trigger, decision.week_used) == ("stop", "week", 75.0)


def _evaluate(monkeypatch, five_used, week_used, held=None, agent=""):
    from hooks.context import account_sessions
    from scripts.routing import reservation
    from scripts.swarm_v2 import quota

    for name in ("AH_ROUTE_API", "AGENTIHOOKS_TARGET", "AGENTIHOOKS_AGENT_NAME"):
        monkeypatch.delenv(name, raising=False)
    if agent:
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", agent)
    monkeypatch.setattr(qp, "_session_windows", lambda session_id: (five_used, week_used, None, None))
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 1)
    monkeypatch.setattr(account_sessions, "session_account", lambda pid: "main")
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda **_: {})
    monkeypatch.setattr(quota, "fleet_observations", lambda environ: {})
    main = qp.Candidate("main", 10.0, 75.0, 0, 0.0, 6)
    monkeypatch.setattr(qp, "_other_accounts", lambda sessions, observed: [main])
    monkeypatch.setattr(qp, "_api_accounts", lambda sessions: [])
    monkeypatch.setattr(qp, "push_active", lambda session_id: False)
    reads, decided = [], []
    monkeypatch.setattr(reservation, "current", lambda harness, environ: reads.append(harness) or held)
    monkeypatch.setattr(qp, "decide_reserved", lambda ceiling, **inputs: decided.append((ceiling, inputs)))
    assert qp.evaluate("s") is None
    return reads, decided


def test_evaluate_reads_no_reservation_below_the_lowest_ceiling(monkeypatch):
    assert _evaluate(monkeypatch, 10.0, 49.9) == ([], [])


def test_evaluate_reads_the_reservation_from_the_lowest_ceiling(monkeypatch):
    assert _evaluate(monkeypatch, 10.0, 50.0) == (["claude"], [])


def test_evaluate_decides_at_the_five_hour_limit(monkeypatch):
    reads, decided = _evaluate(monkeypatch, qp.QUOTA_HANDOFF_5H_PCT, 10.0)
    assert reads == ["claude"]
    assert [ceiling for ceiling, _ in decided] == [None]


def test_evaluate_hands_a_worker_off_at_the_ceiling(monkeypatch):
    reads, decided = _evaluate(monkeypatch, 10.0, 70.0, MASTER_HELD)
    assert reads == ["claude"]
    [(ceiling, inputs)] = decided
    assert (ceiling, inputs["account"], inputs["week_used"]) == (70.0, "main", 70.0)
    assert [c.reserved for c in inputs["others"]] == [True]
    assert _evaluate(monkeypatch, 10.0, 69.9, MASTER_HELD)[1] == []


def test_evaluate_leaves_a_master_on_its_account_at_the_ceiling(monkeypatch):
    assert _evaluate(monkeypatch, 10.0, 70.0, MASTER_HELD, agent="master@1")[1] == []


def _master_at(monkeypatch, week_used, five_used=10.0, logins=(), held=MASTER_HELD):
    from hooks.context import account_sessions
    from scripts.routing import reservation
    from scripts.swarm_v2 import quota

    for name in ("AH_ROUTE_API", "AGENTIHOOKS_TARGET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@1")
    monkeypatch.setenv("AH_ROUTE_INTERACTIVE_main", "1")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setattr(qp, "_session_windows", lambda session_id: (five_used, week_used, None, None))
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 1)
    monkeypatch.setattr(account_sessions, "session_account", lambda pid: "main")
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda **_: {})
    monkeypatch.setattr(quota, "fleet_observations", lambda environ: {})
    others = [
        qp.Candidate("main", five_used, week_used, 1, 0.0, 6, login=True),
        qp.Candidate("token", 10.0, 5.0, 0, 0.0, 6),
        *(qp.Candidate(name, 10.0, week, 0, 0.0, 6, login=True) for name, week in logins),
    ]
    monkeypatch.setattr(qp, "_other_accounts", lambda sessions, observed: others)
    monkeypatch.setattr(qp, "_api_accounts", lambda sessions: [qp.Candidate("api", None, None, 0, 0.0, 4)])
    monkeypatch.setattr(qp, "push_active", lambda session_id: False)
    monkeypatch.setattr(reservation, "current", lambda harness, environ: held)
    return qp.evaluate("s")


def test_a_master_hands_off_at_98_percent_of_the_week_to_the_claude_ai_login_with_the_most_room(monkeypatch):
    assert qp.QUOTA_HANDOFF_WEEK_PCT == 98
    decision = _master_at(monkeypatch, 98.0, logins=[("second", 60.0), ("third", 20.0)])
    assert (decision.action, decision.trigger, decision.account, decision.target.account) == (
        "handoff",
        "week",
        "main",
        "third",
    )
    assert "agentihooks swarm sw handoff DOC --reason quota" in qp.render(decision, "s", "/repo")
    assert _master_at(monkeypatch, 97.9, logins=[("third", 20.0)], held=None) is None


def test_a_master_off_the_reserved_account_is_never_handed_to_a_token_or_the_api(monkeypatch):
    decision = _master_at(monkeypatch, 98.0, held=None)
    assert (decision.action, decision.target) == ("stop", None)
    assert [c.account for c in decision.others] == ["main"]


def test_a_master_on_the_reserved_account_at_3_percent_hands_off_to_an_open_account(monkeypatch):
    from scripts.routing import reservation

    assert reservation.MASTER_LEAVES == 95.0
    decision = _master_at(monkeypatch, 97.0)
    assert (decision.action, decision.trigger, decision.account, decision.target.account) == (
        "handoff",
        "week",
        "main",
        "token",
    )
    assert (decision.week_used, decision.week_limit) == (97.0, 95.0)
    assert "agentihooks swarm sw handoff DOC --reason quota" in qp.render(decision, "s", "/repo")
    assert _master_at(monkeypatch, 97.0, logins=[("second", 20.0)]).target.account == "second"
    assert _master_at(monkeypatch, 94.9) is None


def test_a_master_hands_off_at_99_percent_of_the_five_hour_window_while_another_login_is_free(monkeypatch):
    assert qp.QUOTA_HANDOFF_5H_PCT == 99
    decision = _master_at(monkeypatch, 40.0, 99.0, logins=[("second", 20.0)])
    assert (decision.action, decision.trigger, decision.target.account) == ("handoff", "five_hour", "second")
    assert _master_at(monkeypatch, 40.0, 99.0).target is None
    assert _master_at(monkeypatch, 40.0, 98.9, logins=[("second", 20.0)]) is None


def test_only_masters_are_limited_to_claude_ai_logins():
    token, login = qp.Candidate("token", 10.0, 5.0, 0, 0.0, 6), qp.Candidate("second", 10.0, 5.0, 0, 0.0, 6, login=True)
    assert qp.master_targets([token, login], {"AGENTIHOOKS_AGENT_NAME": "master@a1b2c3-0001"}) == [login]
    assert qp.master_targets([token, login], {"AGENTIHOOKS_AGENT_NAME": "engineer@a1b2c3-0001"}) == [token, login]
    assert qp.master_targets([token, login], {}) == [token, login]


@pytest.mark.parametrize("five_used", [None, 100.0])
@pytest.mark.parametrize("week_used", [10.0, 98.0])
def test_codex_handoff_policy_uses_only_current_and_target_weeks(monkeypatch, five_used, week_used):
    from hooks.context import account_sessions
    from scripts import codex_quota, codex_router
    from scripts.claude_quota_balancer import QuotaWindow
    from scripts.routing import reservation

    current = codex_quota.CodexQuota(100, "pro", QuotaWindow(five_used, 500), QuotaWindow(week_used, 600))
    target = codex_quota.CodexQuota(100, "pro", QuotaWindow(100.0, 500), QuotaWindow(27.0, 600))
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.delenv("AH_ROUTE_API", raising=False)
    monkeypatch.setattr(qp.time, "time", lambda: 100)
    monkeypatch.setattr(codex_quota, "session_quota", lambda environ, session: current)
    monkeypatch.setattr(codex_router.CodexAccountSource, "pool", lambda self, environ: [])
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {"target": target})
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 1)
    monkeypatch.setattr(account_sessions, "session_account", lambda pid: "self")
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda **_: {})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {"target": 0})
    monkeypatch.setattr(reservation, "current", lambda harness, environ: None)
    monkeypatch.setattr(qp, "_api_accounts", lambda sessions: [])
    monkeypatch.setattr(qp, "push_active", lambda session: False)
    observed = qp.evaluate("s")
    if week_used == 10.0:
        assert observed is None
    else:
        assert (observed.action, observed.trigger, observed.five_used, observed.five_reset) == (
            "handoff",
            "week",
            0.0,
            None,
        )
        assert observed.target.account == "target"
        assert observed.target.routing_left == 73.0
