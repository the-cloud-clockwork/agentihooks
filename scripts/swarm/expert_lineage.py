"""The record of an expert lineage: one saved mission, one seat, and the state only code moves.

The master or the operator starts a lineage from a mission document. The mission text is saved once under its
digest, so every expert that ever occupies the lineage's seat reads the same words. A lineage is running until it
ends; this module is the only writer of the lineage records and of the saved missions.
"""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from scripts.inbox.seats import seat_address  # pyright: ignore[reportUnknownVariableType]
from scripts.swarm import expert_launch, expert_mission, naming
from scripts.swarm.store import SwarmError

LINEAGES = "expert-lineages"
MISSIONS = "expert-missions"
SEQUENCE = "expert-seq"
ACTIVE = "expert-running"
PROOFS = "expert-proofs"
LAUNCHES = "expert-launches"
RUNNING = "running"
STOPPED = "stopped"
COMPLETE = "complete"
PROVEN = "mission proof accepted"
NO_RUNNING = "no running expert lineage"
UNFINISHED = (
    "Lineage {lineage} ended on its mission proof, but its completion did not finish. Run "
    "`agentihooks swarm {slug} expert complete <proof>` again with the same proof: it tells the master, keeps the "
    "recap and ends your seat."
)


@dataclass(frozen=True)
class Lineage:
    """One lineage: its id is its seat label, its mission is the address of the saved text."""

    id: str
    seat: str
    mission: str
    title: str
    source: str
    effort: str
    state: str
    started_by: str
    started_at: int
    ended_by: str = ""
    ended_at: int = 0
    note: str = ""


def start(store: Any, slug: str, text: str, source: str, effort: str, by: str, at: int) -> Lineage:
    """Save the mission text and open a running lineage for it; a malformed mission or another effort is refused."""
    found = expert_mission.problems(text)
    if found:
        raise SwarmError("mission refused; fix the document and start again:\n" + "\n".join(f"- {p}" for p in found))
    if effort not in expert_launch.EFFORTS:
        raise SwarmError(f"an expert runs at effort {' or '.join(expert_launch.EFFORTS)}, not {effort}")
    name = naming.seat_label(expert_launch.LANE, int(store.redis.incr(store.key(slug, SEQUENCE))))
    lineage = Lineage(
        id=name,
        seat=seat_address(slug, name),
        mission=expert_mission.address(slug, text),
        title=expert_mission.title(text),
        source=source,
        effort=effort,
        state=RUNNING,
        started_by=by,
        started_at=at,
    )
    with store.redis.pipeline() as pipe:
        pipe.hset(store.key(slug, MISSIONS), expert_mission.digest(text), text)
        pipe.hset(store.key(slug, LINEAGES), name, json.dumps(asdict(lineage)))
        pipe.sadd(store.key(slug, ACTIVE), name)
        pipe.execute()
    return lineage


def get(store: Any, slug: str, name: str) -> Lineage:
    """The lineage of that id; an unknown id is refused."""
    raw = store.redis.hget(store.key(slug, LINEAGES), name)
    if not raw:
        raise SwarmError(f"swarm {slug} has no expert lineage {name}")
    return Lineage(**json.loads(raw))


def runs(store: Any, slug: str, name: str) -> bool:
    """Whether the lineage of that id is running; an unknown id is refused."""
    return get(store, slug, name).state == RUNNING


def running(store: Any, slug: str) -> list[Lineage]:
    """The lineages of the swarm that have not ended, in the order started. The set of their ids bounds the read, so
    code that runs every pass never reads the lineages that ended."""
    names: list[str] = list(store.redis.smembers(store.key(slug, ACTIVE)))
    raws: list[str] = store.redis.hmget(store.key(slug, LINEAGES), names) if names else []
    rows = [Lineage(**json.loads(raw)) for raw in raws]
    return sorted(rows, key=lambda row: (row.started_at, len(row.id), row.id))


def mission(store: Any, slug: str, lineage: Lineage) -> str:
    """The saved mission text of the lineage; a mission that is gone or no longer matches its address is refused."""
    text = store.redis.hget(store.key(slug, MISSIONS), lineage.mission.rpartition("/")[2]) or ""
    if expert_mission.address(slug, text) != lineage.mission:
        raise SwarmError(f"the saved mission of lineage {lineage.id} is missing or does not match {lineage.mission}")
    return text


def saved(store: Any, slug: str, digest: str) -> bool:
    """Whether the swarm holds a saved mission under that digest."""
    return bool(store.redis.hexists(store.key(slug, MISSIONS), digest))


def stop(store: Any, slug: str, name: str, by: str, note: str, at: int) -> Lineage:
    """End a running lineage by its starter's order; a note says why, and a lineage that already ended is refused."""
    if not note.strip():
        raise SwarmError("stopping a lineage needs a note that says why")
    return _end(store, slug, name, lambda found: replace(found, state=STOPPED, ended_by=by, ended_at=at, note=note), "")


def complete(store: Any, slug: str, name: str, by: str, proof: str, at: int) -> Lineage:
    """End a running lineage on the mission proof its expert handed in, and keep that proof; a lineage that already
    ended is refused. The caller has checked the proof."""

    def proven(found: Lineage) -> Lineage:
        return replace(found, state=COMPLETE, ended_by=by, ended_at=at, note=PROVEN)

    return _end(store, slug, name, proven, proof)


def proof(store: Any, slug: str, name: str) -> str:
    """The mission proof the lineage ended on, empty when it holds none."""
    return store.redis.hget(store.key(slug, PROOFS), name) or ""


def launches(store: Any, slug: str, name: str) -> int:
    """How many experts of the lineage launched so far."""
    return int(store.redis.hget(store.key(slug, LAUNCHES), name) or 0)


def launched(store: Any, slug: str, name: str) -> int:
    """Count one more launched expert of the lineage and return its number in the lineage."""
    return int(store.redis.hincrby(store.key(slug, LAUNCHES), name, 1))


def every(store: Any, slug: str) -> list[Lineage]:
    """Every lineage the swarm ever started, in the order of their ids, each read by its id. For a check an operator
    runs: code that runs every pass reads the running ones."""
    count = int(store.redis.get(store.key(slug, SEQUENCE)) or "0")
    names = [naming.seat_label(expert_launch.LANE, number + 1) for number in range(count)]
    raws: list[str | None] = store.redis.hmget(store.key(slug, LINEAGES), names) if names else []
    return [Lineage(**json.loads(raw)) for raw in raws if raw]


def _end(store: Any, slug: str, name: str, change: Callable[[Lineage], Lineage], proof: str) -> Lineage:
    key = store.key(slug, LINEAGES)

    def write(pipe: Any) -> Lineage:
        raw = pipe.hget(key, name)
        if not raw:
            raise SwarmError(f"swarm {slug} has no expert lineage {name}")
        found = Lineage(**json.loads(raw))
        if found.state != RUNNING:
            raise SwarmError(f"expert lineage {name} is {found.state}, not {RUNNING}")
        ended = change(found)
        pipe.multi()
        pipe.hset(key, name, json.dumps(asdict(ended)))
        pipe.srem(store.key(slug, ACTIVE), name)
        if proof:
            pipe.hset(store.key(slug, PROOFS), name, proof)
        return ended

    return store.redis.transaction(write, key, value_from_callable=True)
