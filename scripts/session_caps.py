"""Session cap per agent account, kept in the shared Redis. AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT is the default for
an account without one."""

KEY = "agentihooks:session-caps"
HARNESSES = ("claude", "codex")
MAX_CAP = 50


def _client():
    from scripts.swarm.store import redis_client

    return redis_client()


def _field(account: str, harness: str) -> str:
    if harness not in HARNESSES:
        raise ValueError(f"harness must be one of {', '.join(HARNESSES)}")
    if not account or ":" in account:
        raise ValueError("account must be a plain account name")
    return f"{harness}:{account}"


def stored(harness: str = "claude") -> dict[str, int]:
    """Account -> cap set for ``harness``; empty when the store is unreachable, so every account takes the default."""
    import redis

    try:
        found = _client().hgetall(KEY)
    except (redis.RedisError, OSError):
        return {}
    prefix = f"{harness}:"
    return {
        field.removeprefix(prefix): int(value)
        for field, value in found.items()
        if field.startswith(prefix) and value.isdigit()
    }


def set_cap(account: str, cap: int | None, harness: str = "claude") -> None:
    """Store ``cap`` for the account; None clears it back to the default."""
    field = _field(account, harness)
    if cap is None:
        _client().hdel(KEY, field)
        return
    if not 1 <= cap <= MAX_CAP:
        raise ValueError(f"a session cap is a whole number from 1 to {MAX_CAP}")
    _client().hset(KEY, field, cap)
