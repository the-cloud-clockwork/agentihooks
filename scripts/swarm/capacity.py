import json
from dataclasses import dataclass

from hooks.context import account_sessions
from scripts import claude_quota_balancer as balancer
from scripts import codex_router

LANES = ("eng", "ci", "plan")


@dataclass(frozen=True)
class Account:
    harness: str
    name: str
    state: str
    sessions: int
    five_left: float | None
    week_left: float | None


def _window(window: balancer.QuotaWindow, now: float) -> balancer.QuotaWindow:
    return balancer.QuotaWindow(used=0, resets_at=None) if window.resets_at and window.resets_at <= now else window


def accounts(environ: dict, now: float) -> list[Account]:
    credentials = balancer.discover_credentials(environ)
    if credentials:
        balancer.collect_results(credentials, environ=environ, now=now)
    counts = account_sessions.sessions_by_account()
    results = []
    for _, result in balancer.cached_observations(environ=environ):
        five, week = _window(result.five_hour, now), _window(result.seven_day, now)
        state, _ = balancer._state(result.provider_status, five, week)
        results.append(
            Account("claude", result.account, state, counts.get(result.account, 0), five.remaining, week.remaining)
        )
    known = {row.name for row in results}
    results += [
        Account("claude", name, "UNKNOWN", count, None, None) for name, count in counts.items() if name not in known
    ]
    pool = [account for account in codex_router.routing_pool(environ) if account.signed_in]
    quotas = codex_router.quotas(pool, environ)
    counts = account_sessions.codex_sessions_by_account()
    for account in pool:
        quota = quotas.get(account.name)
        five = _window(quota.five_hour, now) if quota else balancer.QuotaWindow()
        week = _window(quota.seven_day, now) if quota else balancer.QuotaWindow()
        state, _ = balancer._state("allowed", five, week)
        results.append(
            Account("codex", account.name, state, counts.get(account.name, 0), five.remaining, week.remaining)
        )
    return results


def free_seats(account: Account, cap: int, week_floor: float) -> int:
    if account.five_left is None or account.week_left is None:
        return 0
    if account.harness == "codex" and account.week_left < week_floor:
        return 0
    if account.state not in {"NORMAL", "REDUCE", "DRAIN_SOON"}:
        return 0
    if account.state == "DRAIN_SOON" and min(account.five_left, account.week_left) < 20:
        return 0
    limit = cap // 2 if account.state == "REDUCE" else cap
    return max(0, limit - account.sessions)


def _harnesses(config, lane: str) -> tuple[str, ...]:
    requested = config.lanes.get(lane, {}).get("agent")
    if requested in {"claude", "codex"}:
        return (requested,)
    return ("claude",) if config.codex_share == 0 else ("claude", "codex")


def calculate(config, observations: list[Account], agents: list, cap: int, week_floor: float) -> dict:
    configured = dict(zip(LANES, (config.max_eng, config.max_ci, config.max_plan), strict=True))
    effective = {lane: min(configured[lane], sum(a.lane == lane for a in agents)) for lane in LANES}
    placeable = {
        h: sum(free_seats(row, cap, week_floor) for row in observations if row.harness == h)
        for h in ("claude", "codex")
    }
    remaining = dict(placeable)
    while True:
        ready = [
            lane
            for lane in LANES
            if effective[lane] < configured[lane] and any(remaining[h] for h in _harnesses(config, lane))
        ]
        if not ready:
            break
        lane = min(ready, key=lambda name: effective[name])
        harness = next(h for h in _harnesses(config, lane) if remaining[h])
        remaining[harness] -= 1
        effective[lane] += 1
    restricted = sorted({row.state.lower().replace("_", " ") for row in observations if row.state != "NORMAL"})
    reason = "accounts have quota" if not restricted else "accounts are " + ", ".join(restricted)
    reason += f"; Claude has {placeable['claude']} free seats and Codex has {placeable['codex']} free seats"
    return {
        "configured": configured,
        "effective": effective,
        "placeable": placeable,
        "reason": reason,
        "accounts": [row.__dict__ for row in observations],
    }


def read(store, slug: str) -> dict:
    value = store.redis.get(store.key(slug, "quota-capacity"))
    return json.loads(value) if value else {}


def status_line(decision: dict) -> str:
    if not decision:
        return "quota capacity has not been observed"
    caps = decision["effective"]
    return f"quota capacity eng {caps['eng']} ci {caps['ci']} plan {caps['plan']} because {decision['reason']}"


def apply(slug: str, config, store, ledger, runtime, now_ms: int) -> list[str]:
    reader = getattr(runtime, "quota_capacity", None)
    if reader is None:
        return []
    decision = reader(config, store.agents(slug), now_ms / 1000)
    previous = read(store, slug)
    changed = any(previous.get(key) != decision[key] for key in ("configured", "effective", "reason"))
    decision["at"] = now_ms if changed else previous["at"]
    store.redis.set(store.key(slug, "quota-capacity"), json.dumps(decision))
    if not changed:
        return []
    text = status_line(decision)
    rows = ledger.state(slug)["tasks"]
    if rows:
        ledger.comment(slug, rows[0]["id"], text, by=f"quota capacity {now_ms}")
    return [text]
