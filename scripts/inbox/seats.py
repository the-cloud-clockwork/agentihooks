"""Seat registry in Redis: swarm lane slots and the master as addresses `<seat>@<slug>` that outlive their occupants.

Each new occupant bumps the seat's generation; the occupancy history is append-only. Each seat also keeps
append-only recaps and learned notes that its occupants leave for the next.
"""

import json
from dataclasses import dataclass

PREFIX = "agentihooks:seat"
OCCUPY_ATTEMPTS = 5


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

    def history(self, address):
        return [json.loads(entry) for entry in self.redis.lrange(f"{self.key(address)}:history", 0, -1)]


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

    def learn(self, address, occupant, text, at):
        entry = {"occupant": occupant, "text": text, "at": at}
        self.redis.rpush(self.key(address, "learned"), json.dumps(entry))

    def learned(self, address):
        return [json.loads(entry) for entry in self.redis.lrange(self.key(address, "learned"), 0, -1)]


def _occupancy(raw):
    return Occupancy(raw.get("occupant", ""), int(raw.get("generation", 0)))
