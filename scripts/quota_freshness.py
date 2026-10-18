"""The freshness check every drain and cap decision runs on a quota reading before it reads the numbers."""

from collections.abc import Iterable

from scripts import session_bands
from scripts.claude_quota_balancer import _duration

NEVER = "never read"


def stale(observed_at: float | None, resets: Iterable[float | None], now: float) -> bool:
    return not session_bands.fresh(observed_at, now) or any(reset is not None and reset <= now for reset in resets)


def age(observed_at: float | None, now: float) -> str:
    return NEVER if observed_at is None else _duration(int(now), int(observed_at))
