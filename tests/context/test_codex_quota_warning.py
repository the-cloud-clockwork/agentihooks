"""Codex sessions read their own quota, warn once per account, window and reset, and hand off by resuming."""

import json
import shlex
import time
from datetime import datetime, timezone

import pytest

from hooks import hook_manager
from hooks.context import quota_policy as qp
from hooks.targets.emitter import flush

SID = "019a0000-0000-7000-8000-000000000001"
PEER = "019a0000-0000-7000-8000-000000000002"
BETA = "019a0000-0000-7000-8000-000000000003"


def _rollout(home, session_id, five, week, five_reset, week_reset, observed=None):
    day = home / "sessions" / "2026" / "10" / "11"
    day.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(observed or time.time(), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    limits = {
        "limit_id": "codex",
        "plan_type": "plus",
        "primary": {"used_percent": five, "window_minutes": 300, "resets_at": five_reset},
        "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": week_reset},
    }
    event = {"timestamp": stamp, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}}
    with (day / f"rollout-2026-10-11T00-00-00-{session_id}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


def _clock(epoch):
    return datetime.fromtimestamp(epoch).astimezone().strftime("%a %H:%M %Z")


@pytest.fixture
def codex(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("AH_ROUTE_API", raising=False)
    monkeypatch.setattr(qp, "AGENTIHOOKS_HOME", tmp_path / "agentihooks")
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda start=None: 1)
    monkeypatch.setattr("hooks.context.account_sessions.session_account", lambda pid: "alpha")
    return home


def test_a_codex_session_reads_its_windows_from_its_own_rollout(codex):
    now = time.time()
    _rollout(codex, PEER, 99.0, 99.0, int(now + 60), int(now + 60))
    _rollout(codex, SID, 40.0, 91.0, int(now + 3600), int(now + 86400))
    assert qp._session_windows(SID) == (40.0, 91.0, int(now + 3600), int(now + 86400))
    assert qp._session_windows("019a0000-0000-7000-8000-00000000000f") is None


def test_a_stale_codex_reading_is_no_reading(codex):
    now = time.time()
    _rollout(codex, SID, 40.0, 91.0, int(now + 3600), int(now + 86400), observed=now - qp.QUOTA_USAGE_STALE_SEC - 5)
    assert qp._session_windows(SID) is None


def test_a_passed_codex_reset_counts_as_an_empty_window(codex):
    now = time.time()
    _rollout(codex, SID, 97.0, 91.0, int(now - 1), int(now + 86400))
    assert qp._session_windows(SID) == (0.0, 91.0, int(now - 1), int(now + 86400))


def test_controlled_crossings_give_exactly_one_warning_each_and_a_reset_rearms_it(codex):
    now = time.time()
    five_reset, week_reset, next_five = int(now + 3600), int(now + 5 * 86400), int(now + 7200)

    def warn(five, week, five_at=five_reset):
        _rollout(codex, SID, five, week, five_at, week_reset)
        return qp.early_warning(SID)

    assert warn(94.0, 89.0) is None
    week = warn(10.0, 90.0)
    assert week == (
        f"QUOTA WARNING — Codex account alpha has 10% of its 7-day quota left (90% used); it resets "
        f"{qp.reset_when(week_reset)}. Tell the operator now. Nothing is blocked: at 98% used the quota policy "
        f"moves this conversation to another account with a handoff that resumes it."
    )
    assert _clock(week_reset) in week
    assert warn(10.0, 93.0) is None
    five = warn(95.0, 93.0)
    assert "Codex account alpha has 5% of its 5-hour quota left (95% used)" in five
    assert _clock(five_reset) in five and "at 99% used" in five
    assert warn(97.0, 94.0) is None
    assert warn(20.0, 94.0, next_five) is None
    rearmed = warn(96.0, 94.0, next_five)
    assert "4% of its 5-hour quota left" in rearmed and _clock(next_five) in rearmed
    assert warn(98.0, 94.0, next_five) is None


def test_both_windows_crossing_at_once_warn_together_once(codex):
    now = time.time()
    _rollout(codex, SID, 95.0, 90.0, int(now + 3600), int(now + 86400))
    text = qp.early_warning(SID)
    assert text.count("QUOTA WARNING") == 2
    assert "7-day" in text and "5-hour" in text
    assert qp.early_warning(SID) is None


def test_the_hard_threshold_leaves_the_warning_to_the_quota_policy(codex):
    now = time.time()
    _rollout(codex, SID, 99.0, 98.0, int(now + 3600), int(now + 86400))
    assert qp.early_warning(SID) is None


def test_one_warning_per_account_across_its_sessions(codex, monkeypatch):
    now = time.time()
    _rollout(codex, SID, 10.0, 91.0, int(now + 3600), int(now + 86400))
    _rollout(codex, PEER, 10.0, 91.0, int(now + 3600), int(now + 86400))
    assert "Codex account alpha" in qp.early_warning(SID)
    assert qp.early_warning(PEER) is None
    monkeypatch.setattr("hooks.context.account_sessions.session_account", lambda pid: "beta")
    assert "Codex account beta" in qp.early_warning(PEER)


@pytest.mark.parametrize(
    "variable,value", [("AGENTIHOOKS_SWARM", "sw"), ("AGENTIHOOKS_TARGET", "claude"), ("AH_ROUTE_API", "1")]
)
def test_swarm_claude_and_api_sessions_get_no_harness_warning(codex, monkeypatch, variable, value):
    now = time.time()
    _rollout(codex, SID, 10.0, 91.0, int(now + 3600), int(now + 86400))
    monkeypatch.setenv(variable, value)
    assert qp.early_warning(SID) is None


def test_the_api_account_gets_no_subscription_warning(codex, monkeypatch):
    now = time.time()
    _rollout(codex, SID, 10.0, 91.0, int(now + 3600), int(now + 86400))
    monkeypatch.setattr("hooks.context.account_sessions.session_account", lambda pid: "api")
    assert qp.early_warning(SID) is None


@pytest.fixture
def spent(codex, monkeypatch):
    now = time.time()
    _rollout(codex, SID, 10.0, 98.5, int(now + 3600), int(now + 86400))
    _rollout(codex, BETA, 10.0, 30.0, int(now + 3600), int(now + 86400))
    monkeypatch.setenv("AH_CX_TOKEN_beta", "beta-token")
    monkeypatch.setattr(
        "scripts.codex_router._registry", lambda: {SID: {"account": "alpha"}, BETA: {"account": "beta"}}
    )
    monkeypatch.setattr("hooks.context.account_sessions.codex_sessions_by_account", lambda: {"alpha": 1, "beta": 1})
    monkeypatch.setattr("hooks.context.account_sessions.sessions_by_account", lambda: {})
    monkeypatch.setattr("scripts.routing.codex_api.provider", lambda env: "")
    monkeypatch.setattr(qp, "push_active", lambda session: False)
    monkeypatch.setattr(qp, "handed_off_block", lambda session: None)
    return codex


def test_a_spent_codex_session_hands_off_to_a_codex_account_and_resumes_its_conversation(spent, monkeypatch):
    from scripts.init_agent import _parser

    monkeypatch.setattr(
        "scripts.claude_quota_balancer.cached_observations",
        lambda: pytest.fail("a Codex session never reads Claude accounts"),
    )
    decision = qp.evaluate(SID)
    assert (decision.action, decision.trigger, decision.account) == ("handoff", "week", "alpha")
    assert (decision.target.account, decision.target.sessions, decision.target.cap) == ("beta", 1, 6)
    assert [c.account for c in decision.others] == ["alpha", "beta"]
    text = qp.render(decision, SID, "/repo")
    command = next(line.removeprefix("2. Run: ") for line in text.splitlines() if line.startswith("2. Run: "))
    parsed = _parser().parse_args(shlex.split(command)[2:])
    assert (parsed.handoff, parsed.agent, parsed.resume, parsed.claude_args) == (True, "codex", SID, [])
    assert "resumes this conversation" in text


def test_a_codex_stop_names_the_codex_token_variable(spent, monkeypatch):
    monkeypatch.delenv("AH_CX_TOKEN_beta")
    monkeypatch.setattr("scripts.codex_router._registry", lambda: {SID: {"account": "alpha"}})
    decision = qp.evaluate(SID)
    assert decision.action == "stop"
    text = qp.render(decision, SID, "/repo")
    assert "AH_CX_TOKEN_<slug>" in text and "AH_CC_TOKEN_" not in text


def _pre(session_id):
    hook_manager.on_pre_tool_use(
        {"session_id": session_id, "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/repo"}
    )
    flush("PreToolUse")


def _post(session_id):
    hook_manager.on_post_tool_use(
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {"stdout": "a"},
            "cwd": "/repo",
        }
    )
    flush("PostToolUse")


def test_codex_sees_the_warning_once_after_the_tool_call(codex, capsys):
    now = time.time()
    _rollout(codex, SID, 10.0, 91.0, int(now + 3600), int(now + 86400))
    _pre(SID)
    assert "QUOTA WARNING" not in capsys.readouterr().out
    _post(SID)
    assert capsys.readouterr().out.count("Codex account alpha has 9% of its 7-day quota left") == 1
    _pre(SID)
    _post(SID)
    assert "QUOTA WARNING" not in capsys.readouterr().out


def test_codex_sees_the_handoff_directive_after_the_tool_call(spent, capsys):
    _post(SID)
    out = capsys.readouterr().out
    assert "QUOTA HANDOFF REQUIRED" in out and f"--resume {SID}" in out


def test_a_codex_prompt_carries_the_warning_once(codex):
    now = time.time()
    _rollout(codex, SID, 10.0, 91.0, int(now + 3600), int(now + 86400))
    assert "Codex account alpha has 9% of its 7-day quota left" in qp.prompt_context(SID, "/repo")
    assert qp.prompt_context(SID, "/repo") is None


def test_a_claude_tool_call_gets_no_second_policy_directive(spent, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr(qp, "evaluate", lambda session: pytest.fail("Claude reads the policy at PreToolUse only"))
    _post(SID)
    assert "QUOTA" not in capsys.readouterr().out
