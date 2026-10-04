AGENTS = ("claude", "codex")
DEFAULT_PRIORITY = "claude,codex"


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


def choose(requested: str, environ: dict[str, str]) -> tuple[str, str]:
    """(agent, reason): the requested agent, else the first in priority order with quota left."""
    if requested:
        return requested, "requested"
    order = priority(environ)
    empty: list[str] = []
    for agent in order:
        if has_quota(agent, environ) is not False:
            return agent, "priority" if not empty else f"fallthrough: {', '.join(empty)} has no quota"
        empty.append(agent)
    return order[0], "no agent has quota"
