"""Tests for hooks.context.quota_policy — deterministic handoff / wait / stop."""

import time
from unittest.mock import patch

import pytest

from hooks.context import quota_policy as qp


def _c(account, five, week, sessions=0):
    return qp.Candidate(account, five, week, sessions, time.time())


def _decide(five, week, others, push=False, cap=2):
    return qp.decide(
        account="alpha",
        five_used=five,
        week_used=week,
        five_reset=time.time() + 3600,
        week_reset=time.time() + 86400,
        others=others,
        max_sessions=cap,
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
    assert "agentihooks claude-terminal --handoff" in text
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
