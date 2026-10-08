"""Seat registry in Redis: swarm lane slots and the master as addresses `<seat>@<slug>` that outlive their occupants.

Each new occupant bumps the seat's generation; the occupancy history is append-only. Each seat also keeps
append-only recaps and learned notes that its occupants leave for the next; each learned note has a maturity.
Each swarm keeps one culture text that every occupant of every seat reads.
"""

import json
import re
from dataclasses import dataclass

from scripts.swarm import naming
from scripts.swarm.keyspace import ROOT

PREFIX = f"{ROOT}:seat"
OCCUPY_ATTEMPTS = 5
SCAN_PAGE = 1000
MEMORY_KINDS = ("history", "recaps", "learned")
MATURITIES = ("data", "note", "insight", "canon")
DEFAULT_MATURITY = "note"
CANON = "canon"
CULTURE_PREFIX = f"{ROOT}:culture"


class SeatError(RuntimeError):
    pass


@dataclass(frozen=True)
class Occupancy:
    occupant: str
    generation: int


def seat_address(slug, seat):
    return f"{seat}@{slug}"


def is_seat(address):
    return "@" in address and naming.parse(address) is None


def of_swarm(address, slug, names=None):
    """A seat of the swarm, or the name the swarm gave one of its agents; names (the registry) resolves a name's code."""
    if is_seat(address):
        return address.endswith(f"@{slug}")
    if naming.legacy_slug(address) == slug:
        return True
    return names is not None and names.slug_of(address) == slug


def master_of(address, names=None):
    """The master seat of the swarm a seat or a swarm agent's name belongs to, '' for any other address."""
    if is_seat(address):
        return seat_address(address.split("@", 1)[1], "master")
    slug = names.slug_of(address) if names is not None else naming.legacy_slug(address)
    return seat_address(slug, "master") if slug else ""


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

    def seat_of(self, name, reader=None):
        reader = reader if reader is not None else self.redis
        name = naming.NameRegistry(self.redis).resolve(name, reader)
        address = reader.get(f"{PREFIX}-of:{name}") or ""
        occupant = _occupancy(reader.hgetall(self.key(address))) if address else None
        return address if occupant and occupant.occupant == name else ""

    def known_seat(self, name: str) -> str:
        return self.redis.get(f"{PREFIX}-of:{name}") or ""

    def agent_names(self, slug: str) -> list[str]:
        """Every agent the swarm seated: its registered names, and names from before the registry."""
        return [name for name, _ in self.agent_seats(slug)]

    def agent_seats(self, slug: str) -> list[tuple[str, str]]:
        prefix = f"{PREFIX}-of:"
        legacy = self.redis.scan_iter(match=f"{prefix}{slug}-*", count=SCAN_PAGE)
        old = [name for key in legacy if naming.legacy_slug(name := key[len(prefix) :]) == slug]
        named = [row["name"] for row in naming.NameRegistry(self.redis).names(slug)]
        seats = self._known_seats(old + named)
        return list(zip(old, seats)) + [(name, seat) for name, seat in zip(named, seats[len(old) :]) if seat]

    def _known_seats(self, names):
        return [seat or "" for seat in self.redis.mget([f"{PREFIX}-of:{name}" for name in names])] if names else []

    def exits(self, names: list[str]) -> dict[str, dict[str, str]]:
        raw = self.redis.mget([f"{PREFIX}-of:{name}:exit" for name in names]) if names else []
        return {name: json.loads(value or "{}") for name, value in zip(names, raw)}

    def record_exit(self, name: str, seat: str, reason: str) -> None:
        self.redis.set(f"{PREFIX}-of:{name}:exit", json.dumps({"seat": seat, "reason": reason}), nx=True)

    def exit_of(self, name: str) -> dict[str, str]:
        return json.loads(self.redis.get(f"{PREFIX}-of:{name}:exit") or "{}")

    def history(self, address):
        return [json.loads(entry) for entry in self.redis.lrange(f"{self.key(address)}:history", 0, -1)]

    def note(self, address, event, detail, at):
        """An event on the seat's history that leaves its occupant and generation unchanged."""
        seat = self.occupant(address)
        entry = {"generation": seat.generation, "occupant": seat.occupant, "at": at, "event": event, "detail": detail}
        self.redis.rpush(f"{self.key(address)}:history", json.dumps(entry))

    def swarm_keys(self, slug):
        """The swarm's seats with their history and memory, and the seat pointers of its agents."""
        seat = re.compile(rf"{re.escape(PREFIX)}:[^:@]+@{re.escape(slug)}(:({'|'.join(MEMORY_KINDS)}))?")
        keys = [key for key in self.redis.scan_iter(match=f"{PREFIX}:*@{slug}*") if seat.fullmatch(key)]
        agents = sorted(f"{PREFIX}-of:{name}" for name in self.agent_names(slug))
        return sorted(keys) + agents + [f"{key}:exit" for key in agents if self.redis.exists(f"{key}:exit")]


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

    def retire(self, address, number, by, reason, at):
        """Retire learned note `number` (1 based) so no later occupant reads it; its number stays taken."""
        key = self.key(address, "learned")
        raw = self.redis.lindex(key, number - 1) if number > 0 else None
        if raw is None:
            raise SeatError(f"seat {address} has no learned note {number}")
        entry = _entry(raw)
        if "retired" in entry:
            raise SeatError(f"learned note {number} is already retired")
        if not reason.strip():
            raise SeatError("a retirement needs a reason")
        entry = {**entry, "retired": {"by": by, "reason": reason, "at": at}}
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
