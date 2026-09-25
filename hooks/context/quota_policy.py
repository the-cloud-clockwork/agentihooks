"""Quota policy: what a session does when its own quota runs out.

The decision is code, not judgment. Inputs are this session's rate limits (the
statusline snapshot), the router cache for every other account and the live
session count per account. Outcomes:

- handoff: another account has room; move the task there with a handoff.
- wait: only the 5-hour window is spent and the week still has room; set a
  cron for the reset and stop.
- stop: nothing has room; stop and tell the operator.
- push: the operator said "keep pushing"; continue to 100%.
"""

import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hooks.config import (
    AGENTIHOOKS_HOME,
    QUOTA_HANDOFF_5H_PCT,
    QUOTA_HANDOFF_MIN_LEFT,
    QUOTA_HANDOFF_WEEK_PCT,
    QUOTA_WAIT_MIN_WEEK_LEFT,
)

MIN_ROUTING_LEFT = 5.0
PUSH_SIGNALS = ["keep pushing", "push to 100", "push until 100", "burn it to 100"]
WAIT_TOOLS = frozenset({"CronCreate", "CronList", "CronDelete"})


@dataclass(frozen=True)
class Candidate:
    account: str
    five_used: float
    week_used: float
    sessions: int
    observed_at: float

    @property
    def routing_left(self) -> float:
        return max(0.0, 100.0 - max(self.five_used, self.week_used))


@dataclass(frozen=True)
class Decision:
    action: str
    trigger: str
    account: str
    five_used: float
    week_used: float
    five_reset: float | None
    week_reset: float | None
    target: Candidate | None
    others: tuple[Candidate, ...]
    max_sessions: int


def _effective(used: float | None, resets_at: float | None, now: float) -> float | None:
    if used is None:
        return None
    return 0.0 if resets_at is not None and resets_at <= now else used


def decide(
    *,
    account: str,
    five_used: float,
    week_used: float,
    five_reset: float | None,
    week_reset: float | None,
    others: list[Candidate],
    max_sessions: int,
    push: bool,
    five_pct: float = QUOTA_HANDOFF_5H_PCT,
    week_pct: float = QUOTA_HANDOFF_WEEK_PCT,
    min_left: float = QUOTA_HANDOFF_MIN_LEFT,
    wait_min_week_left: float = QUOTA_WAIT_MIN_WEEK_LEFT,
) -> Decision | None:
    week_hit = week_used >= week_pct
    five_hit = five_used >= five_pct
    if not (week_hit or five_hit):
        return None

    pool = [c for c in others if c.account != account and c.five_used < five_pct and c.week_used < week_pct]
    good = [c for c in pool if c.routing_left >= min_left]
    viable = [c for c in pool if c.routing_left >= MIN_ROUTING_LEFT]
    best_good = min(good, key=lambda c: (c.sessions >= max_sessions, -c.routing_left, c.account)) if good else None
    least_bad = min(viable, key=lambda c: (-c.routing_left, c.sessions, c.account)) if viable else None

    def made(action: str, trigger: str, target: Candidate | None = None) -> Decision:
        return Decision(
            action,
            trigger,
            account,
            five_used,
            week_used,
            five_reset,
            week_reset,
            target,
            tuple(sorted(others, key=lambda c: c.account)),
            max_sessions,
        )

    if week_hit:
        trigger = "week"
        target = best_good or least_bad
        if target:
            return made("handoff", trigger, target)
    else:
        trigger = "five_hour"
        if best_good:
            return made("handoff", trigger, best_good)
        if 100.0 - week_used >= wait_min_week_left and five_reset is not None:
            return made("push" if push else "wait", trigger)
        if least_bad:
            return made("handoff", trigger, least_bad)
    return made("push" if push else "stop", trigger)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _session_windows(session_id: str) -> tuple[float, float, float | None, float | None] | None:
    from hooks.context.quota_usage import _load_native

    limits = _load_native(session_id) or {}
    five, week = limits.get("five_hour") or {}, limits.get("seven_day") or {}
    if not isinstance(five.get("used_percentage"), (int, float)) or not isinstance(
        week.get("used_percentage"), (int, float)
    ):
        return None
    now = time.time()
    five_reset = five.get("resets_at") if isinstance(five.get("resets_at"), (int, float)) else None
    week_reset = week.get("resets_at") if isinstance(week.get("resets_at"), (int, float)) else None
    return (
        _effective(float(five["used_percentage"]), five_reset, now),
        _effective(float(week["used_percentage"]), week_reset, now),
        five_reset,
        week_reset,
    )


def _other_accounts(sessions: dict[str, int]) -> list[Candidate]:
    from scripts.claude_quota_balancer import cached_observations

    now = time.time()
    candidates = []
    for observed_at, result in cached_observations():
        five = _effective(result.five_hour.used, result.five_hour.resets_at, now)
        week = _effective(result.seven_day.used, result.seven_day.resets_at, now)
        if five is None or week is None or result.provider_status == "rejected":
            continue
        candidates.append(Candidate(result.account, five, week, sessions.get(result.account, 0), observed_at))
    return candidates


def evaluate(session_id: str) -> Decision | None:
    from hooks.context.account_sessions import agent_pid, max_sessions, session_account, sessions_by_account

    windows = _session_windows(session_id)
    if windows is None:
        return None
    five_used, week_used, five_reset, week_reset = windows
    if five_used < QUOTA_HANDOFF_5H_PCT and week_used < QUOTA_HANDOFF_WEEK_PCT:
        return None
    sessions = sessions_by_account()
    return decide(
        account=session_account(agent_pid()),
        five_used=five_used,
        week_used=week_used,
        five_reset=five_reset,
        week_reset=week_reset,
        others=_other_accounts(sessions),
        max_sessions=max_sessions(),
        push=push_active(session_id),
    )


# ---------------------------------------------------------------------------
# Operator override: "keep pushing"
# ---------------------------------------------------------------------------


def _push_path(session_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session_id)
    return AGENTIHOOKS_HOME / "quota_policy" / f"{safe}.push"


def push_active(session_id: str) -> bool:
    return bool(session_id) and _push_path(session_id).exists()


def record_push_signal(session_id: str, prompt: str) -> bool:
    from hooks.context.ci_manifesto import _signal_match

    if not session_id or not _signal_match(prompt, PUSH_SIGNALS):
        return False
    path = _push_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding="utf-8")
    return True


def clear_session_state(session_id: str) -> None:
    if session_id:
        _push_path(session_id).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Directives
# ---------------------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{value:.0f}%"


def _when(epoch: float | None) -> str:
    if epoch is None:
        return "unknown"
    local = datetime.fromtimestamp(epoch).astimezone()
    span = max(0, int(epoch - time.time()))
    hours, minutes = divmod(span // 60, 60)
    return f"{local.strftime('%a %H:%M %Z')} (in {hours}h{minutes:02d}m)"


def _trigger_text(d: Decision) -> str:
    if d.trigger == "week":
        return f"7-day quota {d.week_used:.1f}% used (limit {QUOTA_HANDOFF_WEEK_PCT:g}%)"
    return f"5-hour quota {d.five_used:.1f}% used (limit {QUOTA_HANDOFF_5H_PCT:g}%)"


def _others_text(d: Decision) -> str:
    rows = [c for c in d.others if c.account != d.account]
    if not rows:
        return "no other account is configured; handoffs need at least 2 accounts"
    return "; ".join(
        f"{c.account} {_pct(c.routing_left)} left (5h {_pct(c.five_used)}, 7d {_pct(c.week_used)} used, "
        f"{c.sessions}/{d.max_sessions} sessions)"
        for c in rows
    )


def _repo_slug(cwd: str) -> str:
    path = Path(cwd or os.getcwd())
    for parent in (path, *path.parents):
        git = parent / ".git"
        if git.is_file():
            # A linked worktree (~/dev/worktrees/<repo>/<name>) keeps its repo name one level up.
            return parent.parent.name
        if git.is_dir():
            return parent.name
    return path.name or "session"


def render(d: Decision, session_id: str, cwd: str) -> str:
    account = d.account or "this login"
    head = f"{_trigger_text(d)} on {account}."
    if d.action == "handoff" and d.target:
        t = d.target
        age = max(0, int(time.time() - t.observed_at)) // 60
        slug = _repo_slug(cwd)
        doc = Path.home() / "scratchpad" / slug / "handoff" / f"{session_id}.md"
        name = f"{slug}-handoff-{time.strftime('%H%M')}"
        return (
            f"QUOTA HANDOFF REQUIRED — {head}\n"
            f"Policy decision (deterministic): move this task to another account now. "
            f"Router cache: {t.account} has {_pct(t.routing_left)} left (5h {_pct(t.five_used)}, "
            f"7d {_pct(t.week_used)} used, {t.sessions}/{d.max_sessions} sessions, observed {age}m ago); "
            f"the new terminal re-probes and picks the final account.\n"
            f"1. Write the handoff document to {doc}: goal, done so far (commits, PRs, evidence), "
            f"in progress, exact next steps, repo/worktree/branch, open risks, the operator's standing "
            f"instructions.\n"
            f'2. Run: agentihooks claude-terminal --handoff --dir "{cwd}" --name "{name}" --prompt-file "{doc}"\n'
            f"3. handoff=done: tell the operator which account and terminal took over, then stop. "
            f"handoff=failed: stop and report the failure to the operator."
        )
    if d.action == "wait":
        reset = (d.five_reset or time.time()) + 120
        at = datetime.fromtimestamp(reset).astimezone()
        cron = f"{at.minute} {at.hour} {at.day} {at.month} *"
        return (
            f"QUOTA WAIT — {head} The 7-day window still has {_pct(100 - d.week_used)} left and no other "
            f"account qualifies ({_others_text(d)}).\n"
            f'1. CronCreate a one-shot job, cron "{cron}" (local time {at.strftime("%a %H:%M")}), recurring '
            f'false, prompt: "The 5-hour quota window has reset. Continue the task from where you stopped."\n'
            f"2. Tell the operator this session waits until the reset at {_when(d.five_reset)}, then stop. "
            f"Every other tool is blocked until the window resets."
        )
    reset = d.week_reset if d.trigger == "week" else d.five_reset
    return (
        f"QUOTA STOP — {head} Resets {_when(reset)}. No other account has room: {_others_text(d)}.\n"
        f"Stop working and tell the operator to add another AH_CC_TOKEN_<slug> account, or to say "
        f'"keep pushing" to continue on this account until 100%. Every tool is blocked.'
    )


def handed_off_block(session_id: str) -> str | None:
    from hooks.context.broadcast import session_status

    info = session_status(session_id)
    if info.get("status") != "handed_off":
        return None
    return (
        f"BLOCKED: this session handed its task off to account {info.get('handed_off_to') or '?'} at "
        f"{info.get('handed_off_at') or '?'}. Stop working here and tell the operator where the work continues."
    )


def pretool(session_id: str, tool_name: str, cwd: str) -> tuple[str | None, str | None]:
    """(block_reason, context) for one tool call."""
    blocked = handed_off_block(session_id)
    if blocked:
        return blocked, None
    d = evaluate(session_id)
    if d is None or d.action == "push":
        return None, None
    text = render(d, session_id, cwd)
    if d.action == "stop" or (d.action == "wait" and tool_name not in WAIT_TOOLS):
        return f"BLOCKED: {text}", None
    return None, text


def prompt_context(session_id: str, cwd: str) -> str | None:
    d = evaluate(session_id)
    if d is None:
        return None
    if d.action == "push":
        return f"QUOTA PUSH — {_trigger_text(d)}; the operator said to keep pushing on this account until 100%."
    return render(d, session_id, cwd)
