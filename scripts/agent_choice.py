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
        from scripts import codex_router

        pool = codex_router.routing_pool(environ)
        seen = codex_router.quotas(pool, environ)
        used = [quota.highest_used for quota in seen.values() if quota and quota.highest_used is not None]
        if not used:
            return None
        threshold = float(environ.get("AGENTIHOOKS_HANDOFF_WEEK_PCT") or 98)
        unknown = any(account.signed_in and seen.get(account.name) is None for account in pool)
        return unknown or any(value < threshold for value in used)
    from scripts.claude_quota_balancer import cached_observations, is_routable

    results = [result for _, result in cached_observations()]
    return any(is_routable(result) for result in results) if results else None


def account_has_quota(agent: str, account: str, environ: dict[str, str]) -> bool | None:
    """True or False for one account of this agent from the last known quota; None when nothing is known."""
    if agent == "codex":
        from scripts import codex_router

        pool = [found for found in codex_router.routing_pool(environ) if found.name == account]
        quota = codex_router.quotas(pool, environ).get(account) if pool else None
        if quota is None or quota.highest_used is None:
            return None
        return quota.highest_used < float(environ.get("AGENTIHOOKS_HANDOFF_WEEK_PCT") or 98)
    from scripts.claude_quota_balancer import cached_observations, is_routable

    seen = [result for _, result in cached_observations() if _account(result) == account]
    return is_routable(seen[-1]) if seen else None


def _account(result) -> str:
    return result.account


def at_cap(agent: str, environ: dict[str, str]) -> bool:
    """True when every account of this agent already runs the maximum number of sessions."""
    from hooks.context import account_sessions

    cap = account_sessions.max_sessions(environ)
    if agent == "codex":
        from scripts import codex_router

        counts = account_sessions.codex_sessions_by_account()
        pool = [account for account in codex_router.routing_pool(environ) if account.signed_in]
        return all(counts.get(account.name, 0) >= cap for account in pool)
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


def codex_week_left(environ: dict[str, str]) -> float | None:
    """The weekly quota left on the best signed-in Codex account; None when nothing is known."""
    from scripts import codex_router

    pool = [account for account in codex_router.routing_pool(environ) if account.signed_in]
    seen = codex_router.quotas(pool, environ)
    left = [100.0 - quota.seven_day.used for quota in seen.values() if quota and quota.seven_day.used is not None]
    return max(left) if left else None


def choose_shared(
    requested: str, environ: dict[str, str], spawns: dict[str, int], share: int, min_week_left: int, choose=choose
) -> tuple[str, str]:
    """Codex while its share of the swarm's spawns is below the target and its week has room, else the priority choice."""
    if requested:
        return choose(requested, environ)
    codex, total = spawns.get("codex", 0), sum(spawns.values())
    if share > 0 and codex * 100 < share * max(total, 1) and not at_cap("codex", environ):
        left = codex_week_left(environ)
        if left is not None and left >= min_week_left:
            return "codex", f"codex share {codex}/{total} below {share}%"
    return choose("", environ)


def choice_kind(reason: str) -> str:
    """share when the share rule decided, overflow when the preferred agent was full or out of quota, forced when requested."""
    if reason == "requested":
        return "forced"
    if reason == "priority" or reason.startswith("codex share "):
        return "share"
    if reason.startswith("fallthrough:"):
        return "overflow"
    return "other"
