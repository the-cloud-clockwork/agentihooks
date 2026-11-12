def stale_only(pool: list, readings: dict, now: float) -> list:
    return [
        account
        for account in pool
        if (reading := readings.get(account.name)) is None
        # ruleid: codex-expired-recheck
        or quota_freshness.stale(reading.observed_at, (reading.five_hour.resets_at, reading.seven_day.resets_at), now)
    ]


def stale_or_recheck(pool: list, readings: dict, environ: dict, now: float) -> list:
    return [
        account
        for account in pool
        if (reading := readings.get(account.name)) is None
        or codex_router.recheck_expired(account, reading, environ, now)
        # ok: codex-expired-recheck
        or quota_freshness.stale(reading.observed_at, (reading.five_hour.resets_at, reading.seven_day.resets_at), now)
    ]


def probe_due(pool: list, found: dict, environ: dict, now: float) -> dict:
    for account in pool:
        seen = found.get(account.name)
        # ok: codex-expired-recheck
        stale = seen is None or quota_freshness.stale(seen.observed_at, (seen.five_hour.resets_at, seen.seven_day.resets_at), now)
        recheck = recheck_expired(account, seen, environ, now)
        found[account.name] = stale or recheck
    return found
