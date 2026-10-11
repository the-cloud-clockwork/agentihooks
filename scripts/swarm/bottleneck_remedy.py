"""Each fresh standing bottleneck finding runs its stage's mechanical remedy within the operator's caps, else proposes
one task to the master: one action per stage per window, logged with the hours it targeted and recorded as the
stage's last action.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from scripts import agent_choice
from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck, bottleneck_status, dispatcher, lane_split, templates
from scripts.swarm.health import standing
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.ledger_events import SENDER, Mail
from scripts.swarm.store import DELEGATE, FULL, RedisStore, SwarmConfig

KEY = "bottleneck-remedies"
RULE = "bottleneck-remedy"
URGENT = "urgent"
SEATS = {"claim_wait": ("ci", "eng")}
HARNESS = ("launch_failure", "quota_hold")
PINNABLE = ("eng", "ci")
FIX = "Bottleneck fix for the {label} stage"
SHIFTED = "Moved one seat from the {giver} lane to the {taker} lane after {targeted}: engineers {eng}, CI {ci}."
MOVED = "Moved the {lane} lane from {source} to {target} after {targeted}."
RAISED = "Raised task {task} to urgent after {targeted}."
PROPOSED = "Proposed a task to the master after {targeted}: {why}."
PROPOSE = (
    "The bottleneck hunter proposes a task for the {label} stage: {targeted}, fired {fired} {times}; {why}. If the "
    "operator agrees, add it titled {fix} and the next window raises it to urgent."
)
UNMECHANICAL = "the stage has no mechanical remedy"
BELOW = "autonomy {autonomy} leaves remedies to the master"
UNPINNED = "no lane is pinned to one harness"


@dataclass(frozen=True)
class Stage:
    name: str
    hours: float
    threshold: float
    fired: int

    @property
    def label(self) -> str:
        return self.name.replace("_", " ")

    @property
    def targeted(self) -> str:
        return f"{self.label} at {self.hours:g} task hours in the window, over its {self.threshold:g} threshold"


def remedy_pass(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    ledger,
    doc: dict,
    lanes: Callable[[], lane_split.Lanes],
    now_ms: int,
) -> list[str]:
    key = store.key(slug, KEY)
    last = store.redis.hgetall(key)
    done = []
    for row in standing.read(store.redis, slug).values():
        if not _due(row, last, now_ms):
            continue
        stage = Stage(row["stage"], row["value"], row["threshold"], row["fired"])
        action = _remedy(slug, config, store, ledger, doc, lanes, stage)
        if action is None:
            continue
        store.redis.hset(key, stage.name, now_ms)
        bottleneck_status.record_action(store, slug, stage.name, action.text, now_ms)
        done.append(action)
    return [action.text for action in done] + dispatcher.log(slug, done, now_ms)


def _due(row: dict, last: dict, now_ms: int) -> bool:
    if now_ms - row["updated_at"] >= standing.STALE_MS:
        return False
    return row["stage"] not in last or now_ms - int(last[row["stage"]]) >= bottleneck.WINDOW_MS


def _remedy(slug, config, store, ledger, doc, lanes, stage):
    fixer = _fixer(doc, stage)
    if fixer is not None and fixer.get("rank") == URGENT:
        return None
    if config.autonomy not in (DELEGATE, FULL):
        return _propose(slug, store, stage, BELOW.format(autonomy=config.autonomy))
    if fixer is not None:
        found, why = _raise(slug, ledger, fixer, stage)
    elif stage.name in SEATS:
        found, why = _shift(slug, config, store, lanes, stage)
    elif stage.name in HARNESS:
        found, why = _move(slug, config, store, stage)
    else:
        found, why = None, UNMECHANICAL
    return found or _propose(slug, store, stage, why)


def _fixer(doc, stage):
    title = FIX.format(label=stage.label)
    return next((t for t in doc["tasks"] if t.get("title", "").startswith(title) and dispatcher._open(t)), None)


def _raise(slug, ledger, task, stage):
    try:
        ledger.rank_task(slug, task["id"], URGENT, dispatcher.AUTHOR, if_owned=True)
    except LedgerRefused as exc:
        return None, f"the ledger kept the rank of task {task['id']}: {exc}"
    text = RAISED.format(task=task["id"], targeted=stage.targeted)
    return dispatcher.Action(RULE, "apply", text + dispatcher._comment(slug, ledger, task, text), task), ""


def _shift(slug, config, store, lanes, stage):
    giver, taker = SEATS[stage.name]
    caps, shift = lane_split._capacity(config, store, slug)
    if reason := lane_split.refusal(caps, giver, taker, lanes()):
        return None, reason
    caps = {giver: caps[giver] - 1, taker: caps[taker] + 1}
    lane_split._apply(slug, config, store, caps, shift + (1 if taker == "ci" else -1))
    text = SHIFTED.format(giver=giver, taker=taker, targeted=stage.targeted, eng=caps["eng"], ci=caps["ci"])
    return dispatcher.Action(RULE, "apply", text, {}), ""


def _move(slug, config, store, stage):
    lanes = {key: dict(value) for key, value in config.lanes.items()}
    lane = next((key for key in PINNABLE if lanes.get(key, {}).get("agent") in agent_choice.AGENTS), None)
    if lane is None:
        return None, UNPINNED
    source = lanes[lane]["agent"]
    target = next(agent for agent in agent_choice.AGENTS if agent != source)
    lanes[lane].update(agent=target, model=templates.AUTO)
    store.update(slug, lanes=templates.lane_map(templates.parse({"name": "remedy", "lanes": lanes})))
    text = MOVED.format(lane=lane, source=source, target=target, targeted=stage.targeted)
    return dispatcher.Action(RULE, "apply", text, {}), ""


def _propose(slug, store, stage, why):
    mail = Mail(InboxStore(store.redis), store, slug)
    text = PROPOSE.format(
        label=stage.label,
        targeted=stage.targeted,
        fired=stage.fired,
        times="time" if stage.fired == 1 else "times",
        why=why,
        fix=FIX.format(label=stage.label),
    )
    mail.inbox.send(SENDER, mail.master, text)
    return dispatcher.Action(RULE, "propose", PROPOSED.format(targeted=stage.targeted, why=why), {})


def step(slug: str, config: SwarmConfig, store: RedisStore, ledger, doc: dict, now_ms: int) -> list[str]:
    return remedy_pass(slug, config, store, ledger, doc, partial(lane_split._lanes, slug, store, doc, now_ms), now_ms)
