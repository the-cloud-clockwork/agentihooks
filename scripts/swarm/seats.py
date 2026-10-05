"""Seat registry in Redis: swarm lane slots and the master as addresses `<seat>@<slug>` that outlive their occupants.

Each new occupant bumps the seat's generation; the occupancy history is append-only.
"""

import json
import time
from dataclasses import dataclass

PREFIX = "agentihooks:seat"


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

    def occupy(self, address, occupant, at=None):
        from redis.exceptions import WatchError

        at = time.time_ns() // 1_000_000 if at is None else at
        key = self.key(address)
        while True:
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


def _occupancy(raw):
    return Occupancy(raw.get("occupant", ""), int(raw.get("generation", 0)))
