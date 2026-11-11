"""Night watch drill: run the night watch in enforcing mode against a scratch proof swarm whose front door master
seat is empty, across the empty seat threshold through the tick's own night watch path, and record the recovery it
took. A swarm that is not a running scratch proof swarm, or whose front door seat a live master holds, is refused
untouched."""

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from scripts.gates.enforcement import NIGHT_WATCH
from scripts.swarm import naming
from scripts.swarm.health import night_watch as night_watch_module
from scripts.swarm.store import SwarmError

KEY = "night-watch-drill"
ENFORCE = "enforce"
REFUSED = "front door respawn refused"
NOT_SCRATCH = "{slug} is not a scratch proof swarm; the night watch drill never runs on a live swarm"
NOT_RUNNING = "scratch swarm {slug} is {state}; start it before the night watch drill"
SEAT_HELD = "a live master holds the front door seat of {slug}; the night watch drill needs an empty seat"

night_watch: Any = night_watch_module
WatchPass = Callable[[str, Any, Any, Any, dict[str, Any], int, bool], list[str]]


@dataclass(frozen=True)
class Drill:
    """One drill against one scratch swarm: its store, ledger, runtime, ledger document and clock."""

    store: Any
    slug: str
    ledger: Any
    runtime: Any
    doc: dict[str, Any]
    now_ms: int
    watch_pass: WatchPass


def refusal(drill: Drill) -> str:
    """Why the drill may not run on this swarm, or an empty string when it may."""
    config: Any = drill.store.config(drill.slug)
    if not config.scratch or not naming.PROOF_RE.fullmatch(drill.slug):
        return NOT_SCRATCH.format(slug=drill.slug)
    if config.state != "running":
        return NOT_RUNNING.format(slug=drill.slug, state=config.state)
    watch = night_watch.Watch(
        drill.store, drill.slug, drill.ledger, drill.runtime, drill.doc, drill.now_ms, False, list
    )
    if night_watch._seat_held(watch):
        return SEAT_HELD.format(slug=drill.slug)
    return ""


def run(drill: Drill) -> dict[str, Any]:
    """Set the night watch gate to enforce and store the record of the passes. A seat stall the night watch already
    observed is kept and passed once now; with none observed, the passes age a new one across the threshold."""
    if why := refusal(drill):
        raise SwarmError(why)
    gates: dict[str, str] = drill.store.config(drill.slug).gates
    drill.store.update(drill.slug, gates={**gates, NIGHT_WATCH.name: ENFORCE})
    observed: int | None = night_watch.read(drill.store, drill.slug).get(night_watch.SEAT, {}).get("since")
    actions: list[str] = []
    start = drill.now_ms if observed is not None else drill.now_ms - night_watch.SEAT_MS
    for at in range(start, drill.now_ms + 1, night_watch.GAP_MS):
        actions += drill.watch_pass(drill.slug, drill.store, drill.ledger, drill.runtime, drill.doc, at, False)
    respawned: int | None = night_watch.read(drill.store, drill.slug).get(night_watch.SEAT, {}).get("respawned")
    record: dict[str, Any] = {
        "swarm": drill.slug,
        "at": drill.now_ms,
        "mode": ENFORCE,
        "observed_since": observed,
        "actions": actions,
        "respawned": respawned,
        "recovered": respawned == drill.now_ms and not any(line.startswith(REFUSED) for line in actions),
    }
    drill.store.redis.set(drill.store.key(drill.slug, KEY), json.dumps(record))
    return record


def command(store: Any, args: argparse.Namespace) -> None:
    """agentihooks swarm SLUG night-watch-drill: print the drill record of one enforcing night watch drill."""
    from scripts.swarm.cli import now_ms
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm_v2.runtime.routed import routed
    from scripts.tick.steps import masters

    ledger: Any = LedgerClient()
    steps: Any = masters
    slug: str = args.slug
    doc: dict[str, Any] = ledger.state(slug)
    drill = Drill(store, slug, ledger, routed(herdr=HerdrRuntime()), doc, now_ms(), steps._night_watch)
    print(json.dumps(run(drill)))
