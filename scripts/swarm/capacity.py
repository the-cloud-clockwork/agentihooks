import json
from dataclasses import dataclass, replace

from hooks.context import account_sessions
from scripts import claude_quota_balancer as balancer
from scripts import codex_router, session_bands

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
    week_resets_at: int | None = None


def _left(window: balancer.QuotaWindow, now: float) -> float | None:
    return session_bands.left(window.used, window.resets_at, now)


def _state(cap: int | None) -> str:
    return "UNKNOWN" if cap is None else "CLOSED" if cap == 0 else "OPEN"


def _claude(environ: dict, now: float) -> list[Account]:
    tokens = balancer.ClaudeTokenSource()
    observed = {result.account: (at, result) for at, result in balancer.cached_observations(environ=environ)}
    fresh, _ = tokens.results(environ, now)
    observed.update({result.account: (now, result) for result in fresh})
    counts = account_sessions.sessions_by_account()
    rows = []
    for at, result in observed.values():
        cap = tokens.cap(result, now) if session_bands.fresh(at, now) else None
        five, week = _left(result.five_hour, now), _left(result.seven_day, now)
        reset = session_bands.upcoming(result.seven_day.resets_at, now)
        rows.append(
            Account("claude", result.account, _state(cap), counts.get(result.account, 0), five, week, cap, reset)
        )
    rows += [
        Account("claude", name, "UNKNOWN", count, None, None) for name, count in counts.items() if name not in observed
    ]
    return rows


def _codex(environ: dict, now: float, refresh: bool) -> list[Account]:
    source = codex_router.CodexAccountSource(refresh=refresh)
    pool = [account for account in source.pool(environ) if account.signed_in]
    counts = account_sessions.codex_sessions_by_account()
    found = source.readings(pool, environ, now)
    known = {account.name for account in pool}
    live = [
        codex_router.CodexAccount(name, f"AH_CX_TOKEN_{name}")
        for name in counts
        if name not in known and name != "default"
    ]
    found.update(codex_router.quotas(live, environ))
    rows = []
    for account in pool + live:
        quota = found.get(account.name)
        cap = codex_router.account_cap(quota, now) if account.name in known else None
        five = _left(quota.five_hour, now) if quota else None
        week = _left(quota.seven_day, now) if quota else None
        reset = session_bands.upcoming(quota.seven_day.resets_at, now) if quota else None
        rows.append(Account("codex", account.name, _state(cap), counts.get(account.name, 0), five, week, cap, reset))
    return rows


def accounts(environ: dict, now: float, refresh: bool = True) -> list[Account]:
    """Every account with its band cap; ``refresh`` probes stale Codex readings, which costs a Codex request."""
    return _claude(environ, now) + _codex(environ, now, refresh)


def seats(rows: list[Account]) -> list[session_bands.Seat]:
    return [
        session_bands.Seat(
            row.harness,
            row.name,
            row.cap,
            row.sessions,
            session_bands.spend_by(row.five_left, row.week_resets_at),
        )
        for row in rows
        if row.cap is not None
    ]


def free_seats(account: Account) -> int:
    return max(0, (account.cap or 0) - account.sessions)


def _harnesses(config, lane: str) -> tuple[str, ...]:
    requested = config.lanes.get(lane, {}).get("agent")
    if requested in {"claude", "codex"}:
        return (requested,)
    return ("claude", "codex")


def _ready_indices(effective: dict, limits: dict, options: dict, cursors: dict, room) -> dict:
    ready = {}
    for lane in LANES:
        if effective[lane] >= limits[lane]:
            continue
        index = next(
            (i for i in range(cursors[lane], len(options[lane])) if any(room(lane, i, h) for h in options[lane][i])),
            None,
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


def _allocate(
    config, effective: dict, limits: dict, open_seats: list, requirements: dict | None, accounts: dict | None = None
) -> tuple:
    allocation = {lane: {"claude": 0, "codex": 0} for lane in LANES}
    placements = {lane: [] for lane in LANES}
    options = requirements or {lane: [_harnesses(config, lane)] * limits[lane] for lane in LANES}
    cursors = dict.fromkeys(LANES, 0)
    held = {(seat.harness, seat.account): seat for seat in open_seats}

    def usable(lane, index, seat):
        allowed = (accounts or {}).get(lane, {}).get(index)
        return allowed is None or (seat.harness, seat.account) in allowed

    def room(lane, index, harness):
        return sum(s.free for s in held.values() if s.harness == harness and usable(lane, index, s))

    while True:
        remaining = {h: sum(s.free for s in held.values() if s.harness == h) for h in ("claude", "codex")}
        ready = _ready_indices(effective, limits, options, cursors, room)
        if not ready:
            return allocation, placements
        lane = min(ready, key=lambda name: effective[name])
        index = ready[lane]
        cursors[lane] = index
        reserved = _reserved(limits, effective, options, cursors)
        eligible = [h for h in options[lane][index] if room(lane, index, h)]
        spare = [h for h in eligible if remaining[h] > reserved[h]] or eligible
        seat = session_bands.pick(s for s in held.values() if s.harness in spare and usable(lane, index, s))
        held[(seat.harness, seat.account)] = replace(seat, sessions=seat.sessions + 1)
        allocation[lane][seat.harness] += 1
        placements[lane].append({"index": index, "harness": seat.harness, "account": seat.account})
        cursors[lane] += 1
        effective[lane] += 1


def calculate(
    config,
    observations: list[Account],
    agents: list,
    demand: dict | None = None,
    requirements: dict | None = None,
    accounts: dict | None = None,
    warned: dict | None = None,
) -> dict:
    configured = dict(zip(LANES, (config.max_eng, config.max_ci, config.max_plan), strict=True))
    busy = {lane: sum(a.lane == lane and a.state != "finished" for a in agents) for lane in LANES}
    effective = {lane: min(configured[lane], busy[lane]) for lane in LANES}
    limits = {
        lane: min(configured[lane], busy[lane] + demand[lane]) if demand is not None else configured[lane]
        for lane in LANES
    }
    warned = warned or {}
    open_rows = [row for row in observations if (row.harness, row.name) not in warned]
    placeable = {h: sum(free_seats(row) for row in open_rows if row.harness == h) for h in ("claude", "codex")}
    allocation, placements = _allocate(config, effective, limits, seats(open_rows), requirements, accounts)
    restricted = sorted({row.state.lower() for row in observations if row.state != "OPEN"})
    reason = "accounts have quota" if not restricted else "accounts are " + ", ".join(restricted)
    reason += f"; Claude has {placeable['claude']} free seats and Codex has {placeable['codex']} free seats"
    reason += "".join(
        f"; {harness} {name} is at its {window} quota warning" for (harness, name), window in warned.items()
    )
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
