"""Room left on each stored claude ai master login, from its own sessions' quota snapshots and the router cache."""

from collections.abc import Iterable

from hooks.context import quota_usage


def _used(value: object, reset: object, now: float) -> float:
    if not isinstance(value, (int, float)):
        return 0.0
    return 0.0 if isinstance(reset, (int, float)) and reset <= now else float(value)


def _readings(account: str) -> list[tuple[float, object, object, object, object]]:
    from scripts.claude_quota_balancer import cached_observations

    found = [
        (observed, r.five_hour.used, r.five_hour.resets_at, r.seven_day.used, r.seven_day.resets_at)
        for observed, r in cached_observations()
        if r.account == account
    ]
    snapshot = quota_usage.latest_snapshot(account)
    if snapshot:
        updated, limits = snapshot
        five, week = limits.get("five_hour") or {}, limits.get("seven_day") or {}
        found.append(
            (
                updated,
                five.get("used_percentage"),
                five.get("resets_at"),
                week.get("used_percentage"),
                week.get("resets_at"),
            )
        )
    return found


def usage(account: str, now: float) -> tuple[float, float, float]:
    """Five hour and week percent used and when they were seen; a login never seen reads as unused."""
    found = _readings(account)
    if not found:
        return 0.0, 0.0, 0.0
    observed, five, five_reset, week, week_reset = max(found, key=lambda reading: reading[0])
    return _used(five, five_reset, now), _used(week, week_reset, now), observed


def left(account: str, now: float) -> float:
    five, week, _ = usage(account, now)
    return 100.0 - max(five, week)


def ranked(accounts: Iterable[str], now: float) -> list[str]:
    return sorted(accounts, key=lambda account: (-left(account, now), account))
