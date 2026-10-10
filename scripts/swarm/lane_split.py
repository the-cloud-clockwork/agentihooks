"""Manual scaling moves the stored caps; auto scaling moves the stored lane shift that `autoscale.calculate` applies."""

import json
from dataclasses import dataclass

from scripts.gates import log as gate_log
from scripts.swarm import bottleneck
from scripts.swarm.store import AUTO_SCALING, DELEGATE, FULL

KEY = "lane-split"
TICKS = 3
AUTHOR = "dispatcher"
RULE = "lane-split"
MOVES = {"ci": ("eng", "ci"), "engineering": ("ci", "eng")}
MOVED = (
    "Moved one seat from the {giver} lane to the {taker} lane after the bottleneck report named {named} {ticks} ticks"
    " running: engineers {eng}, CI {ci}, sum {total}."
)
HELD = "Lane split held after the bottleneck report named {named} three ticks running: {reason}."


@dataclass(frozen=True)
class Lanes:
    ready: dict
    live: dict
    room: int | None


def streak(previous: dict, found: dict) -> dict:
    if not found or found.get("at") == previous.get("at"):
        return previous
    named = found.get("bottleneck", "")
    ticks = previous.get("ticks", 0) + 1 if named == previous.get("named") else 1
    return {"named": named, "ticks": ticks, "at": found["at"]}


def refusal(caps: dict, giver: str, taker: str, lanes: Lanes) -> str:
    if caps[giver] <= 0:
        return f"the {giver} lane has no seat to give"
    if caps[giver] <= 1 and lanes.ready[giver]:
        return f"the {giver} lane keeps its last seat for ready work"
    if caps[giver] <= lanes.live[giver]:
        return f"the {giver} lane keeps a seat for each of its {lanes.live[giver]} live agents"
    if lanes.room is not None and caps[taker] + 1 - lanes.live[taker] > lanes.room:
        return f"the {taker} lane would pass host room {lanes.room}"
    return ""


def _caps(config, store, slug: str) -> dict:
    if config.scaling == AUTO_SCALING:
        stored = json.loads(store.redis.get(store.key(slug, "quota-capacity")) or "{}")
        ceilings = (stored.get("autoscale") or {}).get("ceilings")
        if ceilings:
            return {"eng": ceilings["eng"], "ci": ceilings["ci"]}
    return {"eng": config.max_eng, "ci": config.max_ci}


def _apply(slug: str, config, store, giver: str, taker: str) -> None:
    if config.scaling == AUTO_SCALING:
        store.update(slug, lane_shift=config.lane_shift + (1 if taker == "ci" else -1))
        return
    caps = {"eng": config.max_eng, "ci": config.max_ci}
    store.update(slug, **{f"max_{giver}": caps[giver] - 1, f"max_{taker}": caps[taker] + 1})


def lane_pass(slug: str, config, store, lanes: Lanes, now_ms: int) -> list[str]:
    if config.autonomy not in (DELEGATE, FULL):
        return []
    key = store.key(slug, KEY)
    previous = json.loads(store.redis.get(key) or "{}")
    current = streak(previous, bottleneck.read(store, slug))
    if current is previous:
        return []
    move = MOVES.get(current["named"])
    if move is None or current["ticks"] < TICKS:
        store.redis.set(key, json.dumps(current))
        return []
    store.redis.set(key, json.dumps({**current, "ticks": 0}))
    giver, taker = move
    caps = _caps(config, store, slug)
    reason = refusal(caps, giver, taker, lanes)
    if reason:
        return [HELD.format(named=current["named"], reason=reason)]
    _apply(slug, config, store, giver, taker)
    caps = {giver: caps[giver] - 1, taker: caps[taker] + 1}
    text = MOVED.format(
        giver=giver,
        taker=taker,
        named=current["named"],
        ticks=TICKS,
        eng=caps["eng"],
        ci=caps["ci"],
        total=sum(caps.values()),
    )
    room = "unknown" if lanes.room is None else lanes.room
    gate_log.append(slug, gate_log.Row(now_ms, AUTHOR, "apply", AUTHOR, "", RULE, f"{text} Host room {room}."))
    return [text]
