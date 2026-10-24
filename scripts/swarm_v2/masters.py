"""Master seats of one swarm and the phases each owns. Seat one is the lead, at the single master swarm's address."""

import json
import re
from collections.abc import Callable

from scripts.inbox.seats import SeatRegistry, seat_address
from scripts.swarm.store import MASTER, PREFIX, RedisStore, SwarmError

DEFAULT_COUNT = 1
SAVE_ATTEMPTS = 5
UNNUMBERED = 10_000
HIVES = "hives"
RETIRED = "retired"
RETIRED_AT = "retired-at."
GONE = ("finished", "retiring")
HIVE_KEY = "hive."
HIVE_PAIR = f"a hive owner is set as {HIVE_KEY}<hive id>=<master seat>"
TITLE = re.compile(rf"{MASTER} \d+ · \S+")


class MasterError(SwarmError):
    pass


def seat(index: int) -> str:
    return MASTER if index == 1 else f"{MASTER}-{index}"


def title(slug: str, address: str) -> str:
    """The name a master shows in Claude.ai, herdr and Remote Control: its seat number, then its swarm."""
    number = 1 if address in ("", seat_address(slug, MASTER)) else _number(slug, address)
    return f"{MASTER} {number} · {slug}"


def is_title(text: str) -> bool:
    return TITLE.fullmatch(text) is not None


def is_lead(slug: str, agent) -> bool:
    """A master record with no seat predates seats and holds the lead."""
    return agent.lane == MASTER and agent.seat in ("", seat_address(slug, MASTER))


def seat_lines(slug: str, task: dict) -> list[str]:
    """Opening prompt lines for a master launched in seat two or later; the lead's task carries no masters count."""
    if "masters" not in task:
        return []
    swarm = f"agentihooks swarm {slug}"
    return [
        f"You hold master seat {task['seat']}, one of {task['masters']} masters of swarm {slug}. The lead seat "
        f"{seat_address(slug, MASTER)} answers the operator and his chat; you act on the inbox items for the phases "
        f"and hives you own: {', '.join(task.get('owns', [])) or 'none yet'}.",
        f"The tick assigns phases by code and posts every change on the masters channel. Read it with {swarm} "
        f'masters-channel read and coordinate with the other masters with {swarm} masters-channel say "<text>".',
        f"When the swarm shrinks the tick asks you to retire: write your Handoff v2 for the lead master, run {swarm} "
        "handoff <doc> and stop.",
    ]


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


def held(redis, slug: str, agents: list) -> dict[str, str]:
    """Each live master seat, lead first then by number, with the name holding it. The seat registry decides every
    seat it has an occupant for: live while that occupant has neither left nor finished nor retired nor been asked to
    retire. A master record whose seat the registry does not hold yet counts by its record."""
    from scripts.swarm import master_retire

    registry, table, store = SeatRegistry(redis), MasterSeats(redis), RedisStore(redis)
    gone = {a.name for a in agents if a.state in GONE}
    retired = set(table.retired(slug))
    records = {a.seat or a.name: a.name for a in agents if a.lane == MASTER and a.name not in gone}
    found = {}
    for address in {*seats(slug, table.count(slug)), *records} - retired:
        occupant = registry.occupant(address).occupant
        name = occupant or records.get(address)
        if occupant and (occupant in gone or registry.left(occupant, address)):
            continue
        if name and not master_retire.reason(store, slug, name):
            found[address] = name
    lead = seat_address(slug, MASTER)
    return dict(sorted(found.items(), key=lambda pair: (pair[0] != lead, _number(slug, pair[0]))))


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
        for _ in range(share - _load(owners, added)):
            busiest = max(seat_list, key=lambda s: (_load(owners, s), -rank[s]))
            owners[[p for p in phases if owners[p] == busiest][-1]] = added
    return {p: owners[p] for p in phases}


def _load(owners: dict[str, str], seat_id: str) -> int:
    return sum(owner == seat_id for owner in owners.values())


def _added(held: list[str], address: str) -> list[str]:
    return held if address in held else [*held, address]


def _handed(owned: dict[str, str], address: str, front: str) -> dict[str, str]:
    return {item: front if owner == address else owner for item, owner in owned.items()}


def count_of(text: str) -> int:
    if not text.isdecimal():
        raise MasterError(f"masters takes a whole number of master seats, not {text!r}")
    return _checked(int(text))


def owner_of(target: str, doc: dict, owners: dict[str, str]) -> str:
    kind, _, rest = target.partition("/")
    item = rest.partition("/")[0]
    if kind == HIVES:
        return owners.get(f"{HIVES}/{item}", "")
    if kind in ("questions", "followups"):
        row = next((row for row in doc.get(kind, []) if row.get("id") == item), {})
        if row.get("hive"):
            return owners.get(f"{HIVES}/{row['hive']}", "")
        if not row.get("task"):
            return ""
        kind, item = "tasks", row["task"]
    if kind == "tasks":
        item = {t.get("id"): t.get("phase") for t in doc.get("tasks", [])}.get(item)
    elif kind != "phases":
        return ""
    return owners.get(item, "")


def route(target: str, doc: dict, owners: Callable[[], dict[str, str]], live: list[str], lead: str) -> str:
    if target.partition("/")[0] not in ("phases", "tasks", "questions", "followups", HIVES):
        return lead
    owner = owner_of(target, doc, owners())
    return owner if owner in live else lead


def hive_targets(saved: str | None, current: list[str]) -> dict[str, str]:
    """A hive whose owning seat was removed by a lower count goes back to the lead."""
    hives = json.loads(saved or "{}")
    return {f"{HIVES}/{hive}": owner for hive, owner in hives.items() if owner in current}


def check_hive_owner(slug: str, count: int, hive_id: str, owner: str) -> None:
    if owner not in seats(slug, count):
        raise MasterError(f"{owner} is not a master seat of {slug}, so it cannot own hive {hive_id}")


def hive_pairs(slug: str, pairs: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """hive.<id>=<seat> pairs as (hive id, seat address), and every other pair untouched."""
    hives, rest = [], []
    for pair in pairs:
        key, _, seat_text = pair.partition("=")
        if not key.startswith(HIVE_KEY):
            rest.append(pair)
            continue
        hive = key.removeprefix(HIVE_KEY)
        if not hive or not seat_text:
            raise MasterError(HIVE_PAIR)
        hives.append((hive, seat_address(slug, seat_text.removesuffix(f"@{slug}"))))
    return hives, rest


class MasterSeats:
    def __init__(self, redis) -> None:
        self.redis = redis

    def key(self, slug: str) -> str:
        return f"{PREFIX}:{slug}:masters"

    def set_count(self, slug: str, count: int) -> None:
        self.redis.hset(self.key(slug), "count", _checked(count))
        self.keep_retired(slug, 1, count)

    def keep_retired(self, slug: str, above: int, upto: int) -> None:
        """A retired seat numbered at or below above is to be refilled, and one past upto is no longer counted."""
        held = [address for address in self.retired(slug) if above < _number(slug, address) <= upto]
        if held:
            self.redis.hset(self.key(slug), RETIRED, json.dumps(held))
        else:
            self.redis.hdel(self.key(slug), RETIRED)

    def retire(self, slug: str, address: str, at: int) -> None:
        """Every phase the seat owns moves to the front door in the same write; at outlives a later refill."""
        from redis.exceptions import WatchError

        front, key = seat_address(slug, MASTER), self.key(slug)
        for _ in range(SAVE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    saved = json.loads(pipe.hget(key, "owners") or "{}")
                    held = json.loads(pipe.hget(key, RETIRED) or "[]")
                    hives = json.loads(pipe.hget(key, HIVES) or "{}")
                    pipe.multi()
                    pipe.hset(
                        key,
                        mapping={
                            "owners": json.dumps(_handed(saved, address, front)),
                            HIVES: json.dumps(_handed(hives, address, front)),
                            RETIRED: json.dumps(_added(held, address)),
                            f"{RETIRED_AT}{address}": at,
                        },
                    )
                    pipe.execute()
                    return
                except WatchError:
                    continue
        raise MasterError(f"master seats of {slug} kept changing; seat {address} was not retired")

    def retired_at(self, slug: str, address: str) -> int:
        return int(self.redis.hget(self.key(slug), f"{RETIRED_AT}{address}") or 0)

    def retired(self, slug: str) -> list[str]:
        return json.loads(self.redis.hget(self.key(slug), RETIRED) or "[]")

    def count(self, slug: str) -> int:
        return int(self.redis.hget(self.key(slug), "count") or DEFAULT_COUNT)

    def own_hive(self, slug: str, hive_id: str, owner: str) -> None:
        check_hive_owner(slug, self.count(slug), hive_id, owner)
        hives = json.loads(self.redis.hget(self.key(slug), HIVES) or "{}")
        self.redis.hset(self.key(slug), HIVES, json.dumps({**hives, hive_id: owner}, sort_keys=True))

    def check_hives(self, slug: str, counts: list[int], hives: list[tuple[str, str]]) -> None:
        """Checked against the count a command leaves, before that command writes anything."""
        final = counts[-1] if counts else self.count(slug)
        for hive, owner in hives:
            check_hive_owner(slug, final, hive, owner)

    def hive_owners(self, slug: str) -> dict[str, str]:
        return hive_targets(self.redis.hget(self.key(slug), HIVES), seats(slug, self.count(slug)))

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
                    retired = json.loads(pipe.hget(key, RETIRED) or "[]")
                    counted = seats(slug, int(pipe.hget(key, "count") or DEFAULT_COUNT))
                    current = [address for address in counted if address not in retired]
                    hives = hive_targets(pipe.hget(key, HIVES), current)
                    owners = assign(phases, current, saved, known)
                    if (owners, current) != (saved, known):
                        pipe.multi()
                        pipe.hset(
                            key, mapping={"owners": json.dumps(owners, sort_keys=True), "seats": json.dumps(current)}
                        )
                        pipe.execute()
                    return {**owners, **hives}
                except WatchError:
                    continue
        raise MasterError(f"master seats of {slug} kept changing; phase owners were not saved")
