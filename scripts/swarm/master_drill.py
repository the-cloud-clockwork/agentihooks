"""Master drill: retire the front door master of a running or paused scratch proof swarm through its runtime, leaving
its seat empty, then step the tick's own enforcing night watch pass once a minute on a drill clock that starts thirty
minutes back, and record the minute a successor holds the front door seat and the route it runs on. Only a successor
on the claude ai login inside the thirty minutes is a recovery. A swarm that is not a running or paused scratch proof
swarm is refused untouched; a paused one stays paused."""

import argparse
import json
from typing import Any

from scripts.gates.enforcement import NIGHT_WATCH
from scripts.swarm import lifetime as lifetime_module
from scripts.swarm import naming, reaper, retire_watch
from scripts.swarm.health import night_watch as night_watch_module
from scripts.swarm.health.findings import MINUTE_MS
from scripts.swarm.master_start import seated
from scripts.swarm.night_watch_drill import DRIVEN, ENFORCE, Drill, running
from scripts.swarm.store import MASTER, AgentRecord, SwarmError
from scripts.swarm_v2 import front_door

KEY = "master-drill"
MINUTES = 30
LOGIN = front_door.CLAUDE_AI_LOGIN
NOT_SCRATCH = "{slug} is not a scratch proof swarm; the master drill never runs on a live swarm"
NOT_RUNNING = "scratch swarm {slug} is {state}; the master drill runs on a running or paused scratch swarm"
NOT_RETIRED = (
    "the runtime could not retire the front door master {name} of {slug}; the master drill needs an empty seat"
)

lifetime: Any = lifetime_module
night_watch: Any = night_watch_module


def refusal(drill: Drill) -> str:
    """Why the drill may not run on this swarm, or an empty string when it may."""
    config: Any = drill.store.config(drill.slug)
    if not config.scratch or not naming.PROOF_RE.fullmatch(drill.slug):
        return NOT_SCRATCH.format(slug=drill.slug)
    return "" if config.state in DRIVEN else NOT_RUNNING.format(slug=drill.slug, state=config.state)


def retire(drill: Drill) -> list[str]:
    """Retire every front door master that has not finished through the runtime, or find its hive reports it gone, and
    drop its record, so the seat is empty, and return their names. A master the runtime cannot retire refuses the drill
    and keeps its record; masters retired before it stay retired."""
    retired: list[str] = []
    for agent in drill.store.agents(drill.slug):
        if agent.lane != MASTER or agent.state == "finished" or not front_door.holds(drill.slug, agent.seat):
            continue
        homes = reaper.scratch_homes(drill.slug, agent.task)
        if not retire_watch.retire_unless_gone(drill.store, drill.runtime, agent, homes, drill.now_ms):
            raise SwarmError(NOT_RETIRED.format(name=agent.name, slug=drill.slug))
        drill.store.drop_agent(drill.slug, agent.name)
        retired.append(agent.name)
    return retired


def successor(drill: Drill) -> AgentRecord | None:
    """The master holding the front door seat as seated, working or starting with a ledger write, if any."""
    live: set[str] = drill.runtime.live_names()
    holders = (
        agent
        for agent in drill.store.agents(drill.slug)
        if agent.lane == MASTER and seated(agent, live, drill.doc) and front_door.holds(drill.slug, agent.seat)
    )
    return next(holders, None)


def watch(drill: Drill, store: Any, start: int, asleep: bool) -> tuple[list[str], int | None, AgentRecord | None]:
    """Pass the tick's night watch once a minute on the drill clock from ``start`` until a successor holds the front
    door seat, at most thirty minutes on: the actions it took, the minute the seat was held and the successor holding
    it, or neither."""
    actions: list[str] = []
    for minute in range(MINUTES + 1):
        at = start + minute * MINUTE_MS
        actions += drill.watch_pass(drill.slug, store, drill.ledger, drill.runtime, drill.doc, at, asleep)
        if (found := successor(drill)) is not None:
            return actions, minute, found
    return actions, None, None


def run(drill: Drill) -> dict[str, Any]:
    """Retire the front door master, set the night watch gate to enforce and start its seat episode and respawn budget
    over, keeping its other episodes, then pass the night watch once a minute from thirty minutes back until a
    successor holds the seat; store the record."""
    if why := refusal(drill):
        raise SwarmError(why)
    retired = retire(drill)
    gates: dict[str, str] = drill.store.config(drill.slug).gates
    drill.store.update(drill.slug, gates={**gates, NIGHT_WATCH.name: ENFORCE})
    state: dict[str, Any] = night_watch.read(drill.store, drill.slug)
    state.pop(night_watch.SEAT, None)
    state.pop("respawns", None)
    drill.store.redis.set(drill.store.key(drill.slug, night_watch.KEY), json.dumps(state))
    start = drill.now_ms - MINUTES * MINUTE_MS
    rows = {task["id"]: task for task in drill.doc["tasks"]}
    asleep = lifetime.sleeping(drill.slug, drill.store, rows)
    actions, minute, found = watch(drill, running(drill.store, drill.slug), start, asleep)
    route = front_door.landed(found) if found and found.state == "working" and found.harness == "claude" else ""
    record: dict[str, Any] = {
        "swarm": drill.slug,
        "at": drill.now_ms,
        "from": start,
        "mode": ENFORCE,
        "retired": retired,
        "actions": actions,
        "seated_minute": minute,
        "successor": found.name if found else "",
        "route": route,
        "recovered": route == LOGIN,
    }
    drill.store.redis.set(drill.store.key(drill.slug, KEY), json.dumps(record))
    return record


def command(store: Any, args: argparse.Namespace) -> None:
    """agentihooks swarm SLUG master-drill: print the record of one master drill run through the routed runtime, the
    live login slot check on the workstation."""
    from scripts.swarm.cli import now_ms
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm_v2.runtime.routed import routed
    from scripts.tick.steps import masters

    ledger: Any = LedgerClient()
    steps: Any = masters
    slug: str = args.slug
    doc: dict[str, Any] = {"tasks": ledger.tasks(slug)}
    drill = Drill(store, slug, ledger, routed(herdr=HerdrRuntime()), doc, now_ms(), steps._night_watch)
    print(json.dumps(run(drill)))
