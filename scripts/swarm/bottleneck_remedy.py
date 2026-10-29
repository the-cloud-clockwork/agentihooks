"""Each fresh standing bottleneck finding runs its stage's mechanical remedy within the operator's caps, else proposes
one task to the master: one action per stage per window, logged with the hours it targeted and recorded as the
stage's last action.
"""

import os
from dataclasses import dataclass

from scripts import agent_choice
from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck, bottleneck_status, capacity, dispatcher, stage_cover, templates
from scripts.swarm.health import standing
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.ledger_events import SENDER, Mail
from scripts.swarm.store import DELEGATE, FULL, RedisStore, SwarmError

KEY = "bottleneck-remedies"
RULE = "bottleneck-remedy"
URGENT = "urgent"
HARNESS = ("launch_failure", "quota_hold")
PINNABLE = ("eng", "ci")
HIVE_HARNESS = "claude"
FIX = "Bottleneck fix for the {label} stage"
MOVED = "Moved the {lane} lane from {source} to {target} after {targeted}."
RAISED = "Raised task {task} to urgent after {targeted}."
PROPOSED = "Proposed a task to the master after {targeted}: {why}."
PROPOSE = "The bottleneck hunter proposes a task for the {label} stage: {targeted}, fired {fired} {times}; {why}. {ask}"
ADD = "If the operator agrees, add it titled {fix} and the next window raises it to urgent."
RAISE = "If the operator agrees, raise task {task} titled {fix} to urgent."
UNMECHANICAL = "the stage has no mechanical remedy"
BELOW = "autonomy {autonomy} leaves remedies to the master"
UNKNOWN = "the finding names no single stuck harness"
UNPINNED = "no lane is pinned to {harness}"
UNPLACEABLE = "no eligible host places {target} for the {lane} lane"
REFUSED = "the swarm refused the change: {error}"


@dataclass(frozen=True)
class Stage:
    name: str
    hours: float
    threshold: float
    fired: int
    unit: str
    harnesses: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.name.replace("_", " ")

    @property
    def targeted(self) -> str:
        return f"{self.label} at {self.hours:g} {self.unit}, over its {self.threshold:g} threshold"


def remedy_pass(
    slug: str,
    store: RedisStore,
    ledger,
    doc: dict,
    now_ms: int,
) -> list[str]:
    key = store.key(slug, KEY)
    last = store.redis.hgetall(key)
    done = []
    try:
        for row in standing.read(store.redis, slug).values():
            if not _due(row, last, now_ms) or stage_cover.covered(store, slug, doc, row["stage"]):
                continue
            action = _remedy(slug, store, ledger, doc, _stage(row))
            if action is None:
                continue
            store.redis.hset(key, row["stage"], now_ms)
            bottleneck_status.record_action(store, slug, row["stage"], action.text, now_ms)
            done.append(action)
    finally:
        logged = dispatcher.log(slug, done, now_ms)
    return [action.text for action in done] + logged


def _stage(row: dict) -> Stage:
    labels = row.get("detector", {}).get("labels", [])
    harnesses = tuple(sorted({label for label in labels if label in agent_choice.AGENTS}))
    return Stage(row["stage"], row["value"], row["threshold"], row["fired"], row["unit"], harnesses)


def _due(row: dict, last: dict, now_ms: int) -> bool:
    if now_ms - row["updated_at"] >= standing.STALE_MS or not row["value"] > row["threshold"] > 0:
        return False
    return row["stage"] not in last or now_ms - int(last[row["stage"]]) >= bottleneck.WINDOW_MS


def _remedy(slug, store, ledger, doc, stage):
    config, fixer = store.config(slug), _fixer(doc, stage)
    if config.autonomy not in (DELEGATE, FULL):
        return None if _settled(fixer) else _propose(slug, store, stage, BELOW.format(autonomy=config.autonomy), fixer)
    found = _mechanical(slug, config, store, stage)
    if isinstance(found, dispatcher.Action) or _settled(fixer):
        return _taken(found)
    if fixer is not None:
        found = _raise(slug, ledger, fixer, stage)
    return _taken(found) or _propose(slug, store, stage, found, fixer)


def _taken(found):
    return found if isinstance(found, dispatcher.Action) else None


def _settled(fixer):
    return fixer is not None and fixer.get("rank") == URGENT


def _mechanical(slug, config, store, stage):
    try:
        if stage.name in HARNESS:
            return _move(slug, config, store, stage)
    except SwarmError as exc:
        return REFUSED.format(error=exc)
    return UNMECHANICAL


def _fixer(doc, stage):
    title = FIX.format(label=stage.label)
    return next((t for t in doc["tasks"] if str(t.get("title")).startswith(title) and dispatcher._open(t)), None)


def _raise(slug, ledger, task, stage):
    try:
        ledger.rank_task(slug, task["id"], URGENT, dispatcher.AUTHOR, if_owned=True)
    except LedgerRefused as exc:
        return f"the ledger kept the rank of task {task['id']}: {exc}"
    text = RAISED.format(task=task["id"], targeted=stage.targeted)
    return dispatcher.Action(RULE, "apply", text + dispatcher._comment(slug, ledger, task, text), task)


def _move(slug, config, store, stage):
    lanes = {key: dict(value) for key, value in config.lanes.items()}
    stuck = _stuck(stage, lanes)
    if stuck is None:
        return UNKNOWN
    target = next(agent for agent in agent_choice.AGENTS if agent != stuck)
    lane = next((key for key in PINNABLE if lanes.get(key, {}).get("agent") == stuck), None)
    if lane is None:
        return UNPINNED.format(harness=stuck)
    if stage.name == "quota_hold" and capacity.read(store, slug).get("placeable", {}).get(stuck, 0) > 0:
        return f"{stuck} accounts still have room"
    if not _placeable(store, config, lane, target):
        return UNPLACEABLE.format(target=target, lane=lane)
    source = lanes[lane]["agent"]
    lanes[lane].update(agent=target, model=templates.AUTO)
    store.update(slug, lanes=templates.lane_map(templates.parse({"name": "remedy", "lanes": lanes})))
    text = MOVED.format(lane=lane, source=source, target=target, targeted=stage.targeted)
    return dispatcher.Action(RULE, "apply", text, {})


def _stuck(stage, lanes):
    if len(stage.harnesses) == 1:
        return stage.harnesses[0]
    if stage.name != "quota_hold":
        return None
    pinned = {lanes.get(key, {}).get("agent") for key in PINNABLE} & set(stage.harnesses)
    return pinned.pop() if len(pinned) == 1 else None


def _placeable(store, config, lane, harness):
    if not config.hive_placement:
        return True
    from scripts import herdr_host
    from scripts.hive import placement, registry
    from scripts.hive.launch import ROLE_PROFILES
    from scripts.swarm import controller, local_slots
    from scripts.swarm.commands import hive_id

    profiles = tuple(profile for profile, role in ROLE_PROFILES.items() if role == lane)
    if harness == HIVE_HARNESS and _hive_room(store, lane, profiles):
        return True
    environ = dict(os.environ)
    if controller.deployment_for(config, environ) != controller.HYBRID or not herdr_host.server_running(environ):
        return False
    local = registry.show(store.redis, hive_id())
    occupied = local_slots.held(store, hive_id(), config.slug, "")
    running = sum(agent.lane == lane and local_slots._local_worker(agent) for agent in store.agents(config.slug))
    expected = max(0, getattr(config, f"max_{lane}") - running)
    return (local is None or placement.hosts(lane, local, profiles)) and occupied + expected <= config.local_max_agents


def _hive_room(store, lane, profiles):
    from scripts.hive import launch, placement, registry
    from scripts.swarm import lease

    now = lease.now_ms(store)
    return any(
        registry.live(hive, now) and placement.hosts(lane, hive, profiles) and hive["room"] > 0
        for hive in launch.loaded_hives(store)
    )


def _propose(slug, store, stage, why, fixer):
    mail = Mail(InboxStore(store.redis), store, slug)
    fix = FIX.format(label=stage.label)
    ask = RAISE.format(task=fixer["id"], fix=fix) if fixer else ADD.format(fix=fix)
    times = "time" if stage.fired == 1 else "times"
    text = PROPOSE.format(label=stage.label, targeted=stage.targeted, fired=stage.fired, times=times, why=why, ask=ask)
    mail.inbox.send(SENDER, mail.master, text)
    return dispatcher.Action(RULE, "propose", PROPOSED.format(targeted=stage.targeted, why=why), {})


def step(slug: str, store: RedisStore, ledger, doc: dict, now_ms: int) -> list[str]:
    return remedy_pass(slug, store, ledger, doc, now_ms)
