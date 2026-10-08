import json
from dataclasses import dataclass

from hooks.context import account_sessions
from scripts import claude_quota_balancer as balancer
from scripts import codex_router, session_caps

LANES = ("eng", "ci", "plan")


@dataclass(frozen=True)
class Account:
    harness: str
    name: str
    state: str
    sessions: int
    five_left: float | None
    week_left: float | None
    cap: int | None = None


def _window(window: balancer.QuotaWindow, now: float) -> balancer.QuotaWindow:
    return balancer.QuotaWindow(used=0, resets_at=None) if window.resets_at and window.resets_at <= now else window


def accounts(environ: dict, now: float) -> list[Account]:
    credentials = balancer.discover_credentials(environ)
    fresh = []
    if credentials:
        fresh, _ = balancer.collect_results(credentials, environ=environ, now=now)
    observed = {result.account: result for _, result in balancer.cached_observations(environ=environ)}
    observed.update({result.account: result for result in fresh})
    limits = {harness: session_caps.stored(harness) for harness in ("claude", "codex")}
    counts = account_sessions.sessions_by_account()
    results = []
    for result in observed.values():
        five, week = _window(result.five_hour, now), _window(result.seven_day, now)
        state = balancer.window_state(result.provider_status, five, [week], now)
        results.append(
            Account(
                "claude",
                result.account,
                state,
                counts.get(result.account, 0),
                five.remaining,
                week.remaining,
                limits["claude"].get(result.account),
            )
        )
    known = {row.name for row in results}
    results += [
        Account("claude", name, "UNKNOWN", count, None, None, limits["claude"].get(name))
        for name, count in counts.items()
        if name not in known
    ]
    pool = [account for account in codex_router.routing_pool(environ) if account.signed_in]
    counts = account_sessions.codex_sessions_by_account()
    known = {account.name for account in pool}
    pool += [
        codex_router.CodexAccount(name, f"AH_CX_TOKEN_{name}")
        for name in counts
        if name not in known and name != "default"
    ]
    quotas = codex_router.quotas(pool, environ)
    for account in pool:
        quota = quotas.get(account.name)
        five = _window(quota.five_hour, now) if quota else balancer.QuotaWindow()
        week = _window(quota.seven_day, now) if quota else balancer.QuotaWindow()
        state = balancer.window_state("allowed", five, [week], now)
        results.append(
            Account(
                "codex",
                account.name,
                state,
                counts.get(account.name, 0),
                five.remaining,
                week.remaining,
                limits["codex"].get(account.name),
            )
        )
    return results


def free_seats(account: Account, cap: int, week_floor: float) -> int:
    if account.five_left is None or account.week_left is None:
        return 0
    if account.harness == "codex" and account.week_left < week_floor:
        return 0
    if account.state not in {"NORMAL", "REDUCE", "DRAIN_SOON"}:
        return 0
    if account.state == "DRAIN_SOON" and account.five_left < 20:
        return 0
    cap = cap if account.cap is None else account.cap
    limit = cap // 2 if account.state == "REDUCE" else cap
    return max(0, limit - account.sessions)


def _harnesses(config, lane: str) -> tuple[str, ...]:
    requested = config.lanes.get(lane, {}).get("agent")
    if requested in {"claude", "codex"}:
        return (requested,)
    return ("claude",) if config.codex_share == 0 else ("claude", "codex")


def _ready_indices(effective: dict, limits: dict, remaining: dict, options: dict, cursors: dict) -> dict:
    ready = {}
    for lane in LANES:
        if effective[lane] >= limits[lane]:
            continue
        index = next(
            (i for i in range(cursors[lane], len(options[lane])) if any(remaining[h] for h in options[lane][i])), None
        )
        if index is not None:
            ready[lane] = index
    return ready


def _reserved(limits: dict, effective: dict, options: dict, cursors: dict) -> dict:
    result = {"claude": 0, "codex": 0}
    for lane in LANES:
        future = options[lane][cursors[lane] : cursors[lane] + limits[lane] - effective[lane]]
        for harness in result:
            result[harness] += sum(choice == (harness,) for choice in future)
    return result


def _allocate(config, effective: dict, limits: dict, remaining: dict, requirements: dict | None) -> tuple:
    allocation = {lane: {"claude": 0, "codex": 0} for lane in LANES}
    placements = {lane: [] for lane in LANES}
    options = requirements or {lane: [_harnesses(config, lane)] * limits[lane] for lane in LANES}
    cursors = dict.fromkeys(LANES, 0)
    while True:
        ready = _ready_indices(effective, limits, remaining, options, cursors)
        if not ready:
            return allocation, placements
        lane = min(ready, key=lambda name: effective[name])
        index = ready[lane]
        cursors[lane] = index
        reserved = _reserved(limits, effective, options, cursors)
        eligible = [h for h in options[lane][index] if remaining[h]]
        harness = max(eligible, key=lambda h: remaining[h] - reserved[h])
        remaining[harness] -= 1
        allocation[lane][harness] += 1
        placements[lane].append({"index": index, "harness": harness})
        cursors[lane] += 1
        effective[lane] += 1


def calculate(
    config,
    observations: list[Account],
    agents: list,
    cap: int,
    week_floor: float,
    demand: dict | None = None,
    requirements: dict | None = None,
) -> dict:
    configured = dict(zip(LANES, (config.max_eng, config.max_ci, config.max_plan), strict=True))
    busy = {lane: sum(a.lane == lane and a.state != "finished" for a in agents) for lane in LANES}
    effective = {lane: min(configured[lane], busy[lane]) for lane in LANES}
    limits = {
        lane: min(configured[lane], busy[lane] + demand[lane]) if demand is not None else configured[lane]
        for lane in LANES
    }
    placeable = {
        h: sum(free_seats(row, cap, week_floor) for row in observations if row.harness == h)
        for h in ("claude", "codex")
    }
    allocation, placements = _allocate(config, effective, limits, dict(placeable), requirements)
    restricted = sorted({row.state.lower().replace("_", " ") for row in observations if row.state != "NORMAL"})
    reason = "accounts have quota" if not restricted else "accounts are " + ", ".join(restricted)
    reason += f"; Claude has {placeable['claude']} free seats and Codex has {placeable['codex']} free seats"
    return {
        "configured": configured,
        "effective": effective,
        "placeable": placeable,
        "reason": reason,
        "accounts": [row.__dict__ for row in observations],
        "allocation": allocation,
        "placements": placements,
    }


def read(store, slug: str) -> dict:
    value = store.redis.get(store.key(slug, "quota-capacity"))
    return json.loads(value) if value else {}


def status_line(decision: dict) -> str:
    if not decision:
        return "quota capacity has not been observed"
    caps = decision["effective"]
    return f"quota capacity eng {caps['eng']} ci {caps['ci']} plan {caps['plan']} because {decision['reason']}"


def _prepared(store, slug: str, task: dict) -> dict:
    saved = store.redis.hget(store.key(slug, "launch-assignments"), task["id"])
    return {
        **task,
        "handoff_envelope": store.handoff_envelope(slug, task["id"]),
        "launch_assignment": json.loads(saved) if saved else {},
    }


def apply(slug: str, config, store, ledger, runtime, now_ms: int) -> list[str]:
    reader = getattr(runtime, "quota_capacity", None)
    if reader is None:
        return []
    from scripts.swarm.tick import _claimable, _ended, _launch_order

    doc = ledger.state(slug)
    rows = {task["id"]: task for task in doc["tasks"]}
    ready = {lane: _launch_order(slug, store, _claimable(slug, store, rows, doc, lane)) for lane in LANES}
    demand = {lane: len(tasks) for lane, tasks in ready.items()}
    requirements = None
    if hasattr(runtime, "quota_requirements"):
        prepared = {lane: [_prepared(store, slug, task) for task in tasks] for lane, tasks in ready.items()}
        requirements = runtime.quota_requirements(config, prepared)
    agents = [agent for agent in store.agents(slug) if not _ended(agent, rows)]
    decision = reader(config, agents, now_ms / 1000, demand, requirements)
    decision["tasks"] = {
        ready[lane][slot["index"]]["id"]: slot["harness"]
        for lane, slots in decision["placements"].items()
        for slot in slots
    }
    previous = read(store, slug)
    changed = any(previous.get(key) != decision[key] for key in ("configured", "effective", "reason"))
    decision["at"] = now_ms if changed else previous["at"]
    text = status_line(decision)
    if changed and rows:
        task = next((row for row in rows.values() if not row.get("done")), next(iter(rows.values())))
        if hasattr(ledger, "capacity_comment"):
            ledger.capacity_comment(slug, task["id"], text, now_ms)
        else:
            ledger.comment(slug, task["id"], text, by="swarm")
    store.redis.set(store.key(slug, "quota-capacity"), json.dumps(decision))
    return [text] if changed else []
