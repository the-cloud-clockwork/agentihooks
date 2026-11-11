def account_cap(quota, now, environ=None):
    if quota is None:
        return None
    # ruleid: feature-codex-expired-ages-with-usable
    if quota.expired:
        return 0
    if not usable(quota, now, environ):
        return None
    return week_cap(quota)


def account_cap_fixed(quota, now, environ=None):
    if quota is None:
        return None
    # ok: feature-codex-expired-ages-with-usable
    if not usable(quota, now, environ):
        return None
    if quota.expired:
        return 0
    return week_cap(quota)


def _codex_state(account, quota, now, environ):
    if quota is None:
        return "UNKNOWN"
    # ruleid: feature-codex-expired-ages-with-usable
    if quota.expired:
        return quota.state
    if not codex_router.usable(quota, now, environ):
        return "STALE"
    return quota.state_at(now)


def _codex_state_fixed(account, quota, now, environ):
    if quota is None:
        return "UNKNOWN"
    # ok: feature-codex-expired-ages-with-usable
    if not codex_router.usable(quota, now, environ):
        return "STALE"
    if quota.expired:
        return quota.state
    return quota.state_at(now)
