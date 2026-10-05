"""Seat registry in Redis: swarm lane slots and the master as addresses `<seat>@<slug>` that outlive their occupants.

Each new occupant bumps the seat's generation; the occupancy history is append-only. Each seat also keeps
append-only recaps and learned notes that its occupants leave for the next; each learned note has a maturity.
Each swarm keeps one culture text that every occupant of every seat reads.
"""

import json
import re
from dataclasses import dataclass

PREFIX = "agentihooks:seat"
OCCUPY_ATTEMPTS = 5
MEMORY_KINDS = ("history", "recaps", "learned")
MATURITIES = ("data", "note", "insight", "canon")
DEFAULT_MATURITY = "note"
CANON = "canon"
CULTURE_PREFIX = "agentihooks:culture"


class SeatError(RuntimeError):
    pass


@dataclass(frozen=True)
class Occupancy:
    occupant: str
    generation: int


def seat_address(slug, seat):
    return f"{seat}@{slug}"


def is_seat(address):
    return "@" in address


def of_swarm(address, slug):
    """A seat of the swarm, or the name the swarm gave one of its agents."""
    return address.endswith(f"@{slug}") or re.fullmatch(rf"{re.escape(slug)}-(eng|ci|master)-\d+", address) is not None


def master_of(address):
    """The master seat of the swarm a seat or a swarm agent's name belongs to, '' for any other address."""
    if is_seat(address):
        return seat_address(address.split("@", 1)[1], "master")
    found = re.fullmatch(r"(.+)-(?:eng|ci|master)-\d+", address)
    return seat_address(found.group(1), "master") if found else ""


class SeatRegistry:
    def __init__(self, redis):
        self.redis = redis

    def key(self, address):
        return f"{PREFIX}:{address}"

    def occupy(self, address, occupant, at):
        from redis.exceptions import WatchError

        key = self.key(address)
        for _ in range(OCCUPY_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    generation = self.watch(pipe, address).generation + 1
                    pipe.multi()
                    pipe.hset(key, mapping={"occupant": occupant, "generation": generation})
                    entry = {"generation": generation, "occupant": occupant, "at": at}
                    pipe.rpush(f"{key}:history", json.dumps(entry))
                    pipe.set(f"{PREFIX}-of:{occupant}", address)
                    pipe.execute()
                    return generation
                except WatchError:
                    continue
        raise SeatError(f"seat {address} kept changing; {occupant} could not take it")

    def watch(self, pipe, address):
        pipe.watch(self.key(address))
        return _occupancy(pipe.hgetall(self.key(address)))

    def occupant(self, address):
        return _occupancy(self.redis.hgetall(self.key(address)))

    def seat_of(self, name):
        address = self.redis.get(f"{PREFIX}-of:{name}") or ""
        return address if address and self.occupant(address).occupant == name else ""

    def known_seat(self, name: str) -> str:
        return self.redis.get(f"{PREFIX}-of:{name}") or ""

    def agent_seats(self, slug: str) -> list[tuple[str, str]]:
        prefix = f"{PREFIX}-of:"
        return [
            (name, self.known_seat(name))
            for key in self.redis.scan_iter(match=f"{prefix}{slug}-*")
            if of_swarm(name := key[len(prefix) :], slug)
        ]

    def history(self, address):
        return [json.loads(entry) for entry in self.redis.lrange(f"{self.key(address)}:history", 0, -1)]

    def swarm_keys(self, slug):
        """The swarm's seats with their history and memory, and the seat pointers of its agents."""
        seat = re.compile(rf"{re.escape(PREFIX)}:[^:@]+@{re.escape(slug)}(:({'|'.join(MEMORY_KINDS)}))?")
        keys = [key for key in self.redis.scan_iter(match=f"{PREFIX}:*@{slug}*") if seat.fullmatch(key)]
        pointers = self.redis.scan_iter(match=f"{PREFIX}-of:{slug}-*")
        return sorted(keys) + sorted(key for key in pointers if of_swarm(key.split(":", 2)[2], slug))


class SeatMemory:
    def __init__(self, redis):
        self.redis = redis

    def key(self, address, kind):
        return f"{PREFIX}:{address}:{kind}"

    def add_recap(self, address, occupant, task, text, at):
        entry = {"occupant": occupant, "task": task, "text": text, "at": at}
        self.redis.rpush(self.key(address, "recaps"), json.dumps(entry))

    def recaps(self, address):
        return [json.loads(entry) for entry in reversed(self.redis.lrange(self.key(address, "recaps"), 0, -1))]

    def learn(self, address, occupant, text, at, maturity=DEFAULT_MATURITY):
        _known(maturity)
        entry = {"occupant": occupant, "text": text, "at": at, "maturity": maturity}
        self.redis.rpush(self.key(address, "learned"), json.dumps(entry))

    def learned(self, address):
        return [_entry(raw) for raw in self.redis.lrange(self.key(address, "learned"), 0, -1)]

    def promote(self, address, number, maturity, by, reason, at):
        """Raise learned note `number` (1 based) on the seat to a higher maturity, keeping who and why."""
        _known(maturity)
        key = self.key(address, "learned")
        raw = self.redis.lindex(key, number - 1) if number > 0 else None
        if raw is None:
            raise SeatError(f"seat {address} has no learned note {number}")
        entry = _entry(raw)
        if MATURITIES.index(maturity) <= MATURITIES.index(entry["maturity"]):
            raise SeatError(f"learned note {number} is already {entry['maturity']}; promote only raises it")
        if not reason.strip():
            raise SeatError("a promotion needs a reason")
        step = {"from": entry["maturity"], "to": maturity, "by": by, "reason": reason, "at": at}
        entry = {**entry, "maturity": maturity, "promotions": [*entry.get("promotions", []), step]}
        self.redis.lset(key, number - 1, json.dumps(entry))
        return entry


class SwarmCulture:
    """One shared text per swarm, kept apart from the swarm's own keys so it outlives a swarm remove."""

    def __init__(self, redis):
        self.redis = redis

    def key(self, slug):
        return f"{CULTURE_PREFIX}:{slug}"

    def set(self, slug, text):
        self.redis.set(self.key(slug), text)

    def get(self, slug):
        return self.redis.get(self.key(slug)) or ""


def _known(maturity):
    if maturity not in MATURITIES:
        raise SeatError(f"maturity is one of {', '.join(MATURITIES)}, not {maturity}")


def _entry(raw):
    return {"maturity": DEFAULT_MATURITY, **json.loads(raw)}


def _occupancy(raw):
    return Occupancy(raw.get("occupant", ""), int(raw.get("generation", 0)))
