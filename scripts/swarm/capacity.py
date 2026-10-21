import json
import sys
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from hooks.context import account_sessions
from scripts import claude_quota_balancer as balancer
from scripts import codex_router, quota_state, session_bands
from scripts.routing import claude_api, codex_api, master_account, place
from scripts.routing.slots import API, INTERACTIVE, SUBSCRIPTION
from scripts.swarm import autoscale, freeze, host_budget
from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import AUTO_SCALING, RedisStore, SwarmConfig

if TYPE_CHECKING:
    from scripts.routing.reservation import Reservation

LANES = ("eng", "ci", "plan")
LABELS = {"claude": "Claude", "codex": "Codex"}


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
    kind: str = SUBSCRIPTION
    weight: int | None = None
    five_resets_at: int | None = None
    reserved: str = ""
    observed_at: float | None = field(default=None, compare=False)
    unreserved_cap: int | None = None


def _left(window: balancer.QuotaWindow, now: float) -> float | None:
    return session_bands.left(window.used, window.resets_at, now)


def _state(cap: int | None, left: float | None) -> str:
    return quota_state.state(cap, left)


def _master_snapshot(environ: dict, now: float) -> dict[str, tuple[float, balancer.ProbeResult]]:
    from redis.exceptions import RedisError

    from hooks.context import quota_usage

    try:
        master = master_account.load(environ).get("claude")
    except (OSError, RedisError):
        return {}
    if master is None or master.kind != INTERACTIVE:
        return {}
    found = quota_usage.account_snapshot(master.slug, now)
    if found is None:
        return {}
    at, limits = found
    five, week = (
        balancer.QuotaWindow(window.get("used_percentage"), window.get("resets_at"))
        for window in (limits.get("five_hour") or {}, limits.get("seven_day") or {})
    )
    state, margin = balancer._state("allowed", five, week)
    return {master.slug: (at, balancer.ProbeResult(master.slug, "allowed", state, margin, five, week))}


def _known(environ: dict) -> set[str]:
    filed = {name.removeprefix(balancer.TOKEN_PREFIX) for name in balancer._current_tokens(environ)}
    return filed | set(account_sessions.token_accounts(environ).values())


def _claude(environ: dict, now: float) -> list[Account]:
    tokens = balancer.ClaudeTokenSource()
    observed = {result.account: (at, result) for at, result in balancer.cached_observations(environ=environ)}
    fresh, _ = tokens.results(environ, now)
    observed.update({result.account: (now, result) for result in fresh})
    observed.update(_master_snapshot(environ, now))
    counts = account_sessions.sessions_by_account(environ=environ)
    routed = _known(environ)
    rows = []
    for at, result in observed.values():
        token = environ.get(f"{balancer.TOKEN_PREFIX}{result.account}")
        listed = result.account in routed
        if not listed and (token or not counts.get(result.account)):
            continue
        cap = tokens.cap(result, now) if listed and session_bands.fresh(at, now) else None
        five, week = _left(result.five_hour, now), _left(result.seven_day, now)
        reset = session_bands.upcoming(result.seven_day.resets_at, now)
        five_reset = session_bands.upcoming(result.five_hour.resets_at, now)
        rows.append(
            Account(
                "claude",
                result.account,
                _state(cap, quota_state.claude_left(five, week)),
                counts.get(result.account, 0),
                five,
                week,
                cap,
                reset,
                five_resets_at=five_reset,
                observed_at=at,
            )
        )
    rows += [
        Account("claude", name, "UNKNOWN", count, None, None)
        for name, count in counts.items()
        if name not in observed and name != account_sessions.API_ACCOUNT
    ]
    return rows + _api(claude_api.ClaudeApiSource(counts), "claude", environ, now)


def _api(source, harness: str, environ: dict, now: float) -> list[Account]:
    found, weight = place.api_side(source, harness, environ, now)
    live = source.sessions.get(account_sessions.API_ACCOUNT)
    if not found:
        return [Account(harness, account_sessions.API_ACCOUNT, "CLOSED", live, None, None, 0, kind=API)] if live else []
    return [
        Account(
            harness,
            slot.account,
            _state(slot.cap, None),
            slot.sessions,
            None,
            None,
            slot.cap,
            kind=API,
            weight=weight,
        )
        for slot in found
    ]


def _codex(environ: dict, now: float, refresh: bool) -> list[Account]:
    source = codex_router.CodexAccountSource(refresh=refresh)
    pool = source.pool(environ)
    counts = account_sessions.codex_sessions_by_account()
    found = source.readings(pool, environ, now)
    known = {account.name for account in pool}
    live = codex_router.live_accounts(pool, counts)
    found.update(codex_router.quotas(live, environ))
    rows = []
    for account in pool + live:
        quota = found.get(account.name)
        cap = codex_router.account_cap(quota, now, environ) if account.name in known else None
        observed_at = quota.observed_at if quota else None
        quota = None if quota and quota.expired else quota
        five = _left(quota.five_hour, now) if quota else None
        week = _left(quota.seven_day, now) if quota else None
        reset = session_bands.upcoming(quota.seven_day.resets_at, now) if quota else None
        five_reset = session_bands.upcoming(quota.five_hour.resets_at, now) if quota else None
        rows.append(
            Account(
                "codex",
                account.name,
                _state(cap, week),
                counts.get(account.name, 0),
                five,
                week,
                cap,
                reset,
                five_resets_at=five_reset,
                observed_at=observed_at,
            )
        )
    return rows + _api(codex_api.CodexApiSource(counts), "codex", environ, now)


def _global(environ: dict, now: float) -> list[Account]:
    from scripts.routing import aggregate

    rows = []
    for row in aggregate.current(now).values():
        quota = row["quota"]
        five = session_bands.left(quota["five_hour"]["used"], quota["five_hour"]["resets_at"], now) if quota else None
        week = session_bands.left(quota["seven_day"]["used"], quota["seven_day"]["resets_at"], now) if quota else None
        cap = None
        if quota and session_bands.fresh(quota["observed_at"], now):
            cap = session_bands.week_cap(week) if row["harness"] == "codex" else session_bands.cap(five, week)
        weight = None
        if row["kind"] == API:
            policy = place.policy(row["harness"], environ)
            cap, weight = policy.cap, policy.weight
        rows.append(
            Account(
                row["harness"],
                row["account"],
                _state(cap, quota_state.routing_left(row["harness"], five, week)),
                row["sessions"],
                five,
                week,
                cap,
                session_bands.upcoming(quota["seven_day"]["resets_at"], now) if quota else None,
                kind=row["kind"],
                weight=weight,
                five_resets_at=session_bands.upcoming(quota["five_hour"]["resets_at"], now) if quota else None,
                observed_at=quota["observed_at"] if quota else None,
            )
        )
    return rows


def accounts(environ: dict, now: float, refresh: bool = True) -> list[Account]:
    """Every account with its band cap; ``refresh`` probes stale Codex readings, which costs a Codex request."""
    from scripts.routing import aggregate, reservation

    rows = (
        _global(environ, now) if aggregate.enabled(environ) else _claude(environ, now) + _codex(environ, now, refresh)
    )
    store = reservation._store(environ)
    for harness in LABELS:
        held = reservation.current(harness, environ)
        rows = latch(reserve(rows, held), held, store)
    return rows


def reserve(rows: list[Account], held: "Reservation | None") -> list[Account]:
    if held is None:
        return rows
    return [
        replace(row, state=quota_state.CLOSED, cap=0, reserved=held.reason, unreserved_cap=row.cap)
        if held.closes_row(row.harness, row.kind, row.name, row.week_left)
        else row
        for row in rows
    ]


def latch(rows: list[Account], held: "Reservation | None", store: RedisStore | None) -> list[Account]:
    """Keep the reserved account closed for workers until its week resets."""
    from redis.exceptions import RedisError

    if held is None or store is None:
        return rows
    key = f"{ROOT}:reserved-closed:{held.harness}:{held.slug}"
    index = next(
        (i for i, row in enumerate(rows) if (row.harness, row.name) == (held.harness, held.slug) and row.kind != API),
        None,
    )
    if index is None:
        return rows
    row = rows[index]
    try:
        if row.reserved:
            if row.week_resets_at:
                store.redis.set(key, row.reserved, exat=row.week_resets_at)
            return rows
        reason = store.redis.get(key)
    except (RedisError, OSError):
        return rows
    if not reason:
        return rows
    closed = replace(row, state=quota_state.CLOSED, cap=0, reserved=reason, unreserved_cap=row.cap)
    return [*rows[:index], closed, *rows[index + 1 :]]


def unreserved(rows: list[Account]) -> list[Account]:
    return [
        replace(
            row,
            state=_state(row.unreserved_cap, quota_state.routing_left(row.harness, row.five_left, row.week_left)),
            cap=row.unreserved_cap,
            reserved="",
        )
        if row.reserved
        else row
        for row in rows
    ]


def _seat(row: Account, cap: int) -> session_bands.Seat:
    return session_bands.Seat(
        row.harness,
        row.name,
        cap,
        row.sessions,
        session_bands.spend_by(row.five_left, row.week_resets_at),
        kind=row.kind,
        weight=row.weight,
    )


def offered(rows: list[Account], closed: Collection[tuple[str, str]] = ()) -> list[session_bands.Seat]:
    """Every row as a seat, closed and unknown rows at cap 0, so their live sessions weigh in the api share."""
    return [_seat(row, 0 if row.cap is None or (row.harness, row.name) in closed else row.cap) for row in rows]


def _side(
    offered_seats: list[session_bands.Seat], harness: str, allowed: Callable[[session_bands.Seat], bool]
) -> session_bands.Seat | None:
    api = [seat for seat in offered_seats if seat.harness == harness and seat.kind == API and allowed(seat)]
    pool = [seat for seat in offered_seats if seat.harness == harness and seat.kind != API]
    weight = sum(seat.weight for seat in api if seat.weight)
    pool_live = sum(seat.sessions for seat in pool)
    return place.place(api, [seat for seat in pool if allowed(seat)], weight, pool_live)


def pick(
    offered_seats: Iterable[session_bands.Seat], allowed: Callable[[session_bands.Seat], bool] = lambda seat: True
) -> session_bands.Seat | None:
    """The split side of each harness over every offered seat, then the allowed free seat with the fewest sessions."""
    offered_seats = list(offered_seats)
    harnesses = dict.fromkeys(seat.harness for seat in offered_seats)
    return session_bands.pick(seat for harness in harnesses if (seat := _side(offered_seats, harness, allowed)))


def free_seats(account: Account) -> int:
    return max(0, (account.cap or 0) - account.sessions)


def warning(window: str) -> str:
    return f"is at its {window} quota warning"


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
        seat = pick(held.values(), lambda s: s.harness in spare and usable(lane, index, s))
        held[(seat.harness, seat.account)] = replace(seat, sessions=seat.sessions + 1)
        allocation[lane][seat.harness] += 1
        placements[lane].append({"index": index, "harness": seat.harness, "account": seat.account})
        cursors[lane] += 1
        effective[lane] += 1


def _configured(config) -> dict:
    return dict(zip(LANES, (config.max_eng, config.max_ci, config.max_plan), strict=True))


def _busy(agents: list) -> dict:
    return {lane: sum(a.lane == lane and a.state != "finished" for a in agents) for lane in LANES}


def _open(observations: list[Account], warned: dict) -> list[Account]:
    return [row for row in observations if (row.harness, row.name) not in warned]


def _placeable(rows: list[Account]) -> dict:
    return {
        h: sum(free_seats(row) for row in rows if row.harness == h and row.kind != API) for h in ("claude", "codex")
    }


def no_spawns(since_ms: int) -> int:
    return 0


@dataclass(frozen=True)
class ScaleInputs:
    observations: list[Account]
    agents: list
    demand: dict | None
    host: Callable[[], host_budget.HostSample | host_budget.Room | None]
    previous: dict
    warned: dict = field(default_factory=dict)
    spent: Callable[[int], int] = no_spawns
    now_ms: int = 0


def host_room(config: SwarmConfig, inputs: ScaleInputs) -> dict:
    stored = inputs.previous.get("host") or (inputs.previous.get("autoscale") or {}).get("host") or {}
    previous = stored["room"] if stored.get("room") is not None else stored.get("last")
    found = host_budget.spawn_room(config, inputs.host(), previous)
    host = {"room": found.room, "reason": found.reason, "limit": found.limit, "held": found.held}
    return host if found.room is not None else {**host, "last": previous}


def granted(host: dict, previous: dict, now_ms: int) -> dict:
    """A held room keeps the time it was first granted, so spawns since then still count against it."""
    since = (previous.get("host") or {}).get("granted_at", now_ms) if host["held"] else now_ms
    return {**host, "granted_at": since}


def unspent(host: dict, spent: Callable[[int], int]) -> int | None:
    """The room less the spawns since it was granted, counted as the spawn gate counts them."""
    if host["room"] is None:
        return None
    return max(0, host["room"] - spent(host["granted_at"]))


def autoscaled(config: SwarmConfig, inputs: ScaleInputs, host: dict | None = None) -> tuple[SwarmConfig, dict | None]:
    if config.scaling != AUTO_SCALING:
        return config, None
    host = host or granted(host_room(config, inputs), inputs.previous, inputs.now_ms)
    stored = inputs.previous.get("autoscale") or {}
    previous = {
        "ceilings": stored.get("ceilings") or _configured(config),
        "pending_raise": stored.get("pending_raise") or {"target": None, "ticks": 0},
    }
    demand = inputs.demand or dict.fromkeys(LANES, 0)
    free = _placeable(_open(inputs.observations, inputs.warned))
    decision = autoscale.calculate(
        _busy(inputs.agents), free, unspent(host, inputs.spent), demand, previous, config.lane_shift
    )
    caps = decision["ceilings"]
    scaled = replace(config, max_eng=caps["eng"], max_ci=caps["ci"], max_plan=caps["plan"])
    return scaled, {**decision, "host": host}


def ready_work(slug: str, store, doc: dict) -> tuple[dict, dict]:
    from scripts.swarm import dispatcher
    from scripts.swarm.tick import ready_in_order

    rows = {task["id"]: task for task in doc["tasks"]}
    dispatched = dispatcher.claim_key(store.config(slug), doc)
    return rows, {lane: ready_in_order(slug, store, rows, doc, lane, dispatched) for lane in LANES}


def live_inputs(slug: str, store, ledger, environ: dict, now_ms: int) -> ScaleInputs:
    from scripts.swarm import quota_handoff
    from scripts.swarm.tick import _ended

    rows, ready = ready_work(slug, store, freeze.watched(slug, store, ledger, ledger.state(slug)))
    observations = accounts(environ, now_ms / 1000, refresh=False)
    thresholds = quota_handoff.Thresholds.from_env(environ)
    warned = {
        (row.harness, row.name): window for row in observations if (window := quota_handoff.trigger(row, thresholds))
    }
    agents = [agent for agent in store.agents(slug) if not _ended(agent, rows)]
    demand = {lane: len(tasks) for lane, tasks in ready.items()}
    spent = spawn_counter(store, now_ms)
    host = host_budget.read_host
    if host_budget.deployed(environ):
        from scripts.swarm_v2 import deployed

        host = deployed.worker_host(environ, slug)
    return ScaleInputs(observations, agents, demand, host, read(store, slug), warned, spent, now_ms)


def spawn_counter(store: RedisStore, now_ms: int) -> Callable[[int], int]:
    from scripts.swarm.tick import host_spent

    return lambda since_ms: host_spent(store, since_ms, now_ms)


def feed_spent(runtime, store: RedisStore, now_ms: int) -> None:
    if hasattr(runtime, "quota_spent"):
        runtime.quota_spent(spawn_counter(store, now_ms))


def fixture_inputs(readings: dict) -> ScaleInputs:
    from scripts.swarm.store import AgentRecord

    agents = [
        AgentRecord(f"{lane}-{n}", lane, "") for lane, count in readings.get("live", {}).items() for n in range(count)
    ]
    sample = host_budget.HostSample(**readings["host"])
    return ScaleInputs(
        [Account(**row) for row in readings["accounts"]],
        agents,
        readings.get("demand"),
        lambda: sample,
        readings.get("previous", {}),
    )


def record(row: Account) -> dict:
    """The stored row; pool rows keep their pre-api fields so decisions without an api stay byte identical."""
    dropped = set() if row.kind == API else {"kind", "weight"}
    dropped |= {"unreserved_cap"}
    dropped |= set() if row.reserved else {"reserved"}
    dropped |= set() if row.observed_at is not None else {"observed_at"}
    return {key: value for key, value in row.__dict__.items() if key not in dropped}


def _api_reason(row: Account) -> str:
    weight = "" if row.weight is None else f" at weight {row.weight}"
    return f"; {LABELS[row.harness]} api is {row.state.lower()}{weight}"


def _without_work(lanes: list[str]) -> str:
    if not lanes:
        return ""
    if len(lanes) == 1:
        return f"{lanes[0]} has no more ready tasks; "
    return f"{', '.join(lanes[:-1])} and {lanes[-1]} have no more ready tasks; "


def _worker_seats(config: SwarmConfig, observations: list[Account], agents: list, warned: dict) -> list:
    seats = offered(observations, warned)
    master = config.lanes.get("master")
    if master is None or master.get("agent") == "codex":
        return seats
    if any(
        agent.lane == "master" and agent.state != "finished" and (agent.harness, agent.account) not in warned
        for agent in agents
    ):
        return seats
    reserved = pick(seats, lambda seat: seat.harness == "claude" and seat.kind == SUBSCRIPTION)
    return [replace(seat, cap=seat.cap - 1) if seat == reserved else seat for seat in seats]


def calculate(
    config,
    observations: list[Account],
    agents: list,
    demand: dict | None = None,
    requirements: dict | None = None,
    accounts: dict | None = None,
    warned: dict | None = None,
) -> dict:
    configured = _configured(config)
    busy = _busy(agents)
    effective = {lane: min(configured[lane], busy[lane]) for lane in LANES}
    limits = {
        lane: min(configured[lane], busy[lane] + demand[lane]) if demand is not None else configured[lane]
        for lane in LANES
    }
    warned = warned or {}
    open_rows = _open(observations, warned)
    placeable = _placeable(open_rows)
    allocation, placements = _allocate(
        config, effective, limits, _worker_seats(config, observations, agents, warned), requirements, accounts
    )
    restricted = sorted({row.state.lower() for row in observations if row.state != "OPEN" and row.kind != API})
    idle = [lane for lane in LANES if effective[lane] == limits[lane] < configured[lane]]
    reason = _without_work(idle)
    reason += "accounts have quota" if not restricted else "accounts are " + ", ".join(restricted)
    reason += f"; Claude has {placeable['claude']} free seats and Codex has {placeable['codex']} free seats"
    reason += "".join(f", {harness} {name} {warning(window)}" for (harness, name), window in warned.items())
    reason += "".join(_api_reason(row) for row in observations if row.kind == API)
    return {
        "configured": configured,
        "effective": effective,
        "placeable": placeable,
        "reason": reason,
        "accounts": [record(row) for row in observations],
        "allocation": allocation,
        "placements": placements,
    }


def read(store, slug: str) -> dict:
    value = store.redis.get(store.key(slug, "quota-capacity"))
    return json.loads(value) if value else {}


def caps(decision: dict, config: SwarmConfig) -> dict[str, int]:
    return decision.get("effective", {"eng": config.max_eng, "ci": config.max_ci, "plan": config.max_plan})


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
    from scripts.swarm.ledger_client import LedgerRefused
    from scripts.swarm.tick import _ended

    rows, ready = ready_work(slug, store, freeze.watched(slug, store, ledger, ledger.state(slug)))
    demand = {lane: len(tasks) for lane, tasks in ready.items()}
    requirements = None
    if hasattr(runtime, "quota_requirements"):
        prepared = {lane: [_prepared(store, slug, task) for task in tasks] for lane, tasks in ready.items()}
        requirements = runtime.quota_requirements(config, prepared)
    agents = [agent for agent in store.agents(slug) if not _ended(agent, rows)]
    previous = read(store, slug)
    if hasattr(runtime, "quota_previous"):
        runtime.quota_previous(previous)
    feed_spent(runtime, store, now_ms)
    decision = reader(config, agents, now_ms / 1000, demand, requirements)
    decision["tasks"] = {
        ready[lane][slot["index"]]["id"]: slot["harness"]
        for lane, slots in decision["placements"].items()
        for slot in slots
    }
    changed = any(previous.get(key) != decision[key] for key in ("configured", "effective", "reason"))
    decision["at"] = now_ms if changed else previous["at"]
    decision["observed_at"] = now_ms
    text = status_line(decision)
    store.redis.set(store.key(slug, "quota-capacity"), json.dumps(decision))
    if changed and rows:
        task = next((row for row in rows.values() if not row.get("done")), next(iter(rows.values())))
        try:
            if hasattr(ledger, "capacity_comment"):
                ledger.capacity_comment(slug, task["id"], text, now_ms)
            else:
                ledger.comment(slug, task["id"], text, by="swarm")
        except LedgerRefused as exc:
            print(f"swarm notice dropped, the ledger refused it: {exc}", file=sys.stderr)
    return [text] if changed else []
