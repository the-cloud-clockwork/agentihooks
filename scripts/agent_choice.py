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
        cap = codex_router.account_cap(quota, time.time())
        return None if cap is None else bool(cap)
    from scripts.claude_quota_balancer import account_cap, cached_observations

    now = time.time()
    seen = [(at, result) for at, result in cached_observations() if _account(result) == account]
    cap = account_cap(seen[-1][1]) if seen and session_bands.fresh(seen[-1][0], now) else None
    return None if cap is None else bool(cap)


def _account(result) -> str:
    return result.account


def choose(requested: str, environ: dict[str, str]) -> tuple[str, str]:
    """(agent, reason): the requested agent, else the harness of the account the session rotation picks next."""
    if requested:
        return requested, "requested"
    from scripts.swarm import capacity

    rows = capacity.accounts(dict(environ), time.time())
    seat = capacity.pick(capacity.offered(rows))
    return (seat.harness, "rotation") if seat else (fallback(rows), ALL_FULL)


def fallback(rows: list) -> str:
    """The harness holding one of ``rows``; empty when no harness holds an account."""
    held = {row.harness for row in rows}
    return next((agent for agent in AGENTS if agent in held), "")


def preferring(agent: str, among: tuple[str, ...] = AGENTS) -> tuple[str, ...]:
    return tuple(sorted(among, key=lambda found: found != agent))


def choice_kind(reason: str) -> str:
    if reason == "requested":
        return "forced"
    if reason == "rotation":
        return "rotation"
    if reason.startswith("fallthrough:"):
        return "overflow"
    return "other"
