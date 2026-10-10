"""Master seats of one swarm and the phases each owns. Seat one is the lead at the address a single master swarm uses."""

import json

from scripts.inbox.seats import seat_address
from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import MASTER, SwarmError

PREFIX = f"{ROOT}:masters"
DEFAULT_COUNT = 1
SAVE_ATTEMPTS = 5


class MasterError(SwarmError):
    pass


def seat(index: int) -> str:
    return MASTER if index == 1 else f"{MASTER}-{index}"


def seats(slug: str, count: int) -> list[str]:
    if count < 1:
        raise MasterError(f"a swarm needs at least one master seat, not {count}")
    return [seat_address(slug, seat(index)) for index in range(1, count + 1)]


def assign(phases: list[str], seat_list: list[str], previous: dict[str, str]) -> dict[str, str]:
    """Each phase keeps a seat that still exists; an unowned phase, in ledger order, goes to the seat owning fewest."""
    owners = {p: previous[p] for p in phases if previous.get(p) in seat_list}
    load = {s: 0 for s in seat_list}
    for owner in owners.values():
        load[owner] += 1
    for phase in phases:
        if phase not in owners:
            owners[phase] = min(seat_list, key=lambda s: (load[s], seat_list.index(s)))
            load[owners[phase]] += 1
    return {p: owners[p] for p in phases}


def owner_of(target: str, doc: dict, owners: dict[str, str]) -> str:
    kind, _, rest = target.partition("/")
    item = rest.split("/", 1)[0]
    if kind == "tasks":
        item = next((t.get("phase", "") for t in doc.get("tasks", []) if t.get("id") == item), "")
    elif kind != "phases":
        return ""
    return owners.get(item, "")


def route(target: str, doc: dict, owners: dict[str, str], live: list[str], lead: str) -> str:
    owner = owner_of(target, doc, owners)
    return owner if owner in live else lead


class MasterSeats:
    def __init__(self, redis):
        self.redis = redis

    def key(self, slug: str) -> str:
        return f"{PREFIX}:{slug}"

    def set_count(self, slug: str, count: int) -> None:
        seats(slug, count)
        self.redis.hset(self.key(slug), "count", count)

    def count(self, slug: str) -> int:
        return int(self.redis.hget(self.key(slug), "count") or DEFAULT_COUNT)

    def owners(self, slug: str, doc: dict) -> dict[str, str]:
        from redis.exceptions import WatchError

        phases = [p["id"] for p in doc.get("phases", []) if p.get("id")]
        key = self.key(slug)
        for _ in range(SAVE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    saved = json.loads(pipe.hget(key, "owners") or "{}")
                    count = int(pipe.hget(key, "count") or DEFAULT_COUNT)
                    owners = assign(phases, seats(slug, count), saved)
                    if owners != saved:
                        pipe.multi()
                        pipe.hset(key, "owners", json.dumps(owners, sort_keys=True))
                        pipe.execute()
                    return owners
                except WatchError:
                    continue
        raise MasterError(f"master seats of {slug} kept changing; phase owners were not saved")
