import time

from scripts import session_bands

AGENTS = ("claude", "codex")
ALL_FULL = "every account is at its session cap"


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


def choose(requested: str, environ: dict[str, str]) -> tuple[str, str]:
    """(agent, reason): the requested agent, else the harness of the account the session rotation picks next."""
    if requested:
        return requested, "requested"
    from scripts.swarm import capacity

    seat = session_bands.pick(capacity.seats(capacity.accounts(dict(environ), time.time())))
    return (seat.harness, "rotation") if seat else ("claude", ALL_FULL)


def choice_kind(reason: str) -> str:
    if reason == "requested":
        return "forced"
    if reason == "rotation":
        return "rotation"
    if reason.startswith("fallthrough:"):
        return "overflow"
    return "other"
