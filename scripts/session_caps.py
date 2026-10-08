"""Session cap per agent account, kept in the shared Redis. AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT is the default for
an account without one."""

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field

from scripts.swarm.keyspace import ROOT

KEY = f"{ROOT}:session-caps"
HARNESSES = ("claude", "codex")
MAX_CAP = 50
ACCOUNT_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,199}")


@dataclass(frozen=True)
class SessionCaps:
    default: int
    stored: Mapping[str, int] = field(default_factory=dict)

    def of(self, account: str) -> int:
        return self.stored.get(account, self.default)


def _client():
    from scripts.swarm.store import redis_client

    return redis_client()


def check(account: str, cap: int | None, harness: str) -> str:
    """The store field for a valid cap; ValueError naming what is wrong otherwise."""
    if harness not in HARNESSES:
        raise ValueError(f"harness must be one of {', '.join(HARNESSES)}")
    if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account):
        raise ValueError("account must be an exact account name")
    if cap is not None and (type(cap) is not int or not 1 <= cap <= MAX_CAP):
        raise ValueError(f"a session cap is a whole number from 1 to {MAX_CAP}")
    return f"{harness}:{account}"


def stored(harness: str = "claude") -> dict[str, int]:
    """Account -> cap set for ``harness``; empty when the store is unreachable, so every account takes the default."""
    import redis

    try:
        found = _client().hgetall(KEY)
    except (redis.RedisError, OSError) as exc:
        sys.stderr.write(f"session caps: store unreachable ({exc}); every account takes the default cap\n")
        return {}
    prefix = f"{harness}:"
    return {
        name.removeprefix(prefix): int(value)
        for name, value in found.items()
        if name.startswith(prefix) and value.isdigit()
    }


def caps(default: int, harness: str = "claude") -> SessionCaps:
    return SessionCaps(default, stored(harness))


def set_cap(account: str, cap: int | None, harness: str = "claude") -> None:
    """Store ``cap`` for the account; None clears it back to the default."""
    name = check(account, cap, harness)
    if cap is None:
        _client().hdel(KEY, name)
    else:
        _client().hset(KEY, name, cap)
