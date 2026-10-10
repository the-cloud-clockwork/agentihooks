"""Master seats of one swarm and the phases each owns. Seat one is the lead, at the single master swarm's address."""

import json
from collections.abc import Callable

from scripts.inbox.seats import seat_address
from scripts.swarm.store import MASTER, PREFIX, SwarmError

DEFAULT_COUNT = 1
SAVE_ATTEMPTS = 5
UNNUMBERED = 10_000


class MasterError(SwarmError):
    pass


def seat(index: int) -> str:
    return MASTER if index == 1 else f"{MASTER}-{index}"


def seats(slug: str, count: int) -> list[str]:
    return [seat_address(slug, seat(index)) for index in range(1, _checked(count) + 1)]


def _checked(count: int) -> int:
    if count < 1:
        raise MasterError(f"a swarm needs at least one master seat, not {count}")
    return count


def lead_of(slug: str, live: list[str]) -> str:
    """The lead seat while it is live, else the lowest numbered live master; the empty lead only when none is live."""
    lead = seat_address(slug, MASTER)
    return lead if lead in live or not live else min(live, key=lambda address: _number(slug, address))


def _number(slug: str, address: str) -> int:
    found = address.removesuffix(f"@{slug}").removeprefix(f"{MASTER}-")
    return int(found) if found.isdecimal() else UNNUMBERED


def live_seats(live: list) -> list[str]:
    return [a.seat or a.name for a in live if a.lane == MASTER]


def assign(phases: list[str], seat_list: list[str], previous: dict[str, str], known: list[str]) -> dict[str, str]:
    """A phase keeps a seat that still exists; an unowned phase, in ledger order, goes to the seat owning fewest.
    A seat not in known (added since the last save) then takes the busiest seats' last phases up to an even share."""
    owners = {p: previous[p] for p in phases if previous.get(p) in seat_list}
    rank = {s: i for i, s in enumerate(seat_list)}
    for phase in phases:
        if phase not in owners:
            owners[phase] = min(seat_list, key=lambda s: (_load(owners, s), rank[s]))
    share = len(phases) // len(seat_list)
    for added in [s for s in seat_list if s not in known]:
        while _load(owners, added) < share:
            busiest = max(seat_list, key=lambda s: (_load(owners, s), -rank[s]))
            owners[[p for p in phases if owners[p] == busiest][-1]] = added
    return {p: owners[p] for p in phases}


def _load(owners: dict[str, str], seat_id: str) -> int:
    return sum(owner == seat_id for owner in owners.values())


def count_of(text: str) -> int:
    if not text.isdecimal():
        raise MasterError(f"masters takes a whole number of master seats, not {text!r}")
    return _checked(int(text))


def owner_of(target: str, doc: dict, owners: dict[str, str]) -> str:
    kind, _, rest = target.partition("/")
    item = rest.partition("/")[0]
    if kind == "tasks":
        item = {t.get("id"): t.get("phase") for t in doc.get("tasks", [])}.get(item)
    elif kind != "phases":
        return ""
    return owners.get(item, "")


def route(target: str, doc: dict, owners: Callable[[], dict[str, str]], live: list[str], lead: str) -> str:
    """owners is read only for a phase or task target, so other items write no owner state."""
    if target.partition("/")[0] not in ("phases", "tasks"):
        return lead
    owner = owner_of(target, doc, owners())
    return owner if owner in live else lead


def seat_lines(slug: str, task: dict) -> list[str]:
    """Opening prompt lines for a master the scaling pass launched in seat two or later; any other task gets none."""
    if "masters" not in task:
        return []
    swarm = f"agentihooks swarm {slug}"
    return [
        f"You hold master seat {task['seat']}, one of {task['masters']} masters of swarm {slug}. The lead seat "
        f"{seat_address(slug, MASTER)} answers the operator and his chat; you act on the inbox items for the phases "
        f"you own: {', '.join(task.get('phases', [])) or 'none yet'}.",
        f"The tick assigns phases by code and posts every change on the masters channel. Read it with {swarm} "
        f'masters-channel read and coordinate with the other masters with {swarm} masters-channel say "<text>".',
        f"When the swarm shrinks the tick asks you to retire: write your Handoff v2 for the lead master, run {swarm} "
        "handoff <doc> and stop.",
    ]


class MasterSeats:
    def __init__(self, redis) -> None:
        self.redis = redis

    def key(self, slug: str) -> str:
        return f"{PREFIX}:{slug}:masters"

    def set_count(self, slug: str, count: int) -> None:
        self.redis.hset(self.key(slug), "count", _checked(count))

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
                    known = json.loads(pipe.hget(key, "seats") or "[]")
                    current = seats(slug, int(pipe.hget(key, "count") or DEFAULT_COUNT))
                    owners = assign(phases, current, saved, known)
                    if (owners, current) != (saved, known):
                        pipe.multi()
                        pipe.hset(
                            key, mapping={"owners": json.dumps(owners, sort_keys=True), "seats": json.dumps(current)}
                        )
                        pipe.execute()
                    return owners
                except WatchError:
                    continue
        raise MasterError(f"master seats of {slug} kept changing; phase owners were not saved")
