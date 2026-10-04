AGENTS = ("claude", "codex")
DEFAULT_PRIORITY = "claude,codex"
ALL_FULL = "every agent is at its session cap"


def priority(environ: dict[str, str]) -> list[str]:
    raw = environ.get("AGENTIHOOKS_AGENT_PRIORITY") or DEFAULT_PRIORITY
    order = [name.strip() for name in raw.split(",") if name.strip() in AGENTS]
    return order or DEFAULT_PRIORITY.split(",")


def has_quota(agent: str, environ: dict[str, str]) -> bool | None:
    """True or False from the last known quota; None when nothing is known."""
    if agent == "codex":
        from scripts.codex_quota import latest_codex_quota

        quota = latest_codex_quota(environ)
        used = quota.highest_used if quota else None
        threshold = float(environ.get("AGENTIHOOKS_HANDOFF_WEEK_PCT") or 98)
        return None if used is None else used < threshold
    from scripts.claude_quota_balancer import cached_observations, is_routable

    results = [result for _, result in cached_observations()]
    return any(is_routable(result) for result in results) if results else None


def _account(result) -> str:
    return result.account


def at_cap(agent: str, environ: dict[str, str]) -> bool:
    """True when every account of this agent already runs the maximum number of sessions."""
    from hooks.context import account_sessions

    cap = account_sessions.max_sessions(environ)
    if agent == "codex":
        return account_sessions.live_codex_sessions() >= cap
    from scripts import claude_quota_balancer as balancer

    accounts = {_account(result) for _, result in balancer.cached_observations() if balancer.is_routable(result)}
    counts = account_sessions.sessions_by_account()
    return bool(accounts) and all(counts.get(account, 0) >= cap for account in accounts)


def choose(requested: str, environ: dict[str, str]) -> tuple[str, str]:
    """(agent, reason): the requested agent, else the first in priority order with quota and a free session slot."""
    if requested:
        return requested, "requested"
    order = priority(environ)
    skipped: list[str] = []
    for agent in order:
        if has_quota(agent, environ) is False:
            skipped.append(f"{agent} has no quota")
        elif at_cap(agent, environ):
            skipped.append(f"{agent} is at its session cap")
        else:
            return agent, "priority" if not skipped else f"fallthrough: {', '.join(skipped)}"
    if any("session cap" in reason for reason in skipped):
        return order[0], ALL_FULL
    return order[0], "no agent has quota"
