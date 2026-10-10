"""Master seats of one swarm and the phases each owns. Seat one is the lead at the address a single master swarm uses."""

import json

from scripts.inbox.seats import seat_address
from scripts.swarm.store import MASTER, PREFIX, SwarmError

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


def live_seats(live: list) -> list[str]:
    return [a.seat or a.name for a in live if a.lane == MASTER]


def assign(phases: list[str], seat_list: list[str], previous: dict[str, str]) -> dict[str, str]:
    """A phase keeps a seat that still exists; an unowned phase, in ledger order, goes to the seat owning fewest. Then
    the last phase of the busiest seat moves to the idlest until loads differ by at most one, so an added seat shares."""
    owners = {p: previous[p] for p in phases if previous.get(p) in seat_list}
    rank = {s: i for i, s in enumerate(seat_list)}
    for phase in phases:
        if phase not in owners:
            owners[phase] = min(seat_list, key=lambda s: (_load(owners, s), rank[s]))
    while True:
        busiest = max(seat_list, key=lambda s: (_load(owners, s), -rank[s]))
        idlest = min(seat_list, key=lambda s: (_load(owners, s), rank[s]))
        if _load(owners, busiest) - _load(owners, idlest) <= 1:
            return {p: owners[p] for p in phases}
        owners[[p for p in phases if owners[p] == busiest][-1]] = idlest


def _load(owners: dict[str, str], seat_id: str) -> int:
    return sum(owner == seat_id for owner in owners.values())


def count_of(text: str) -> int:
    if not text.isdigit():
        raise MasterError(f"masters takes a whole number of master seats, not {text!r}")
    seats("", int(text))
    return int(text)


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
    def __init__(self, redis) -> None:
        self.redis = redis

    def key(self, slug: str) -> str:
        return f"{PREFIX}:{slug}:masters"

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
