"""The masters channel of one swarm: only its master seats post and read; posts stay until the swarm is removed."""

import json
from dataclasses import asdict, dataclass

from scripts.inbox.seats import SeatRegistry
from scripts.swarm.store import PREFIX
from scripts.swarm_v2.masters import MasterError, MasterSeats, seats

CHANNEL_PREFIX = "masters."
SWARM = "swarm"


@dataclass(frozen=True)
class Post:
    number: int
    seat: str
    by: str
    text: str
    at: float


def channel_name(slug: str) -> str:
    return f"{CHANNEL_PREFIX}{slug}"


def reserved(channel: str) -> bool:
    return channel.startswith(CHANNEL_PREFIX)


class MastersChannel:
    def __init__(self, redis) -> None:
        self.redis = redis

    def key(self, slug: str) -> str:
        return f"{PREFIX}:{slug}:masters-channel"

    def member(self, slug: str, name: str) -> str:
        """The master seat name holds now; anyone else is refused, a former master included."""
        held = SeatRegistry(self.redis).seat_of(name)
        if not held or held not in seats(slug, MasterSeats(self.redis).count(slug)):
            raise MasterError(
                f"only master seats of {slug} read and write its masters channel; {name} holds {held or 'no seat'}"
            )
        return held

    def post(self, slug: str, name: str, text: str, at: float) -> Post:
        if not text.strip():
            raise MasterError("a masters channel post needs text")
        return self._append(slug, self.member(slug, name), name, text, at)

    def announce(self, slug: str, text: str, at: float) -> Post:
        """A post by the swarm tick itself, which holds no seat."""
        return self._append(slug, SWARM, SWARM, text, at)

    def _append(self, slug: str, seat: str, by: str, text: str, at: float) -> Post:
        entry = Post(self.redis.incr(f"{self.key(slug)}:count"), seat, by, text, at)
        self.redis.rpush(self.key(slug), json.dumps(asdict(entry)))
        return entry

    def read(self, slug: str, name: str) -> list[Post]:
        self.member(slug, name)
        return self._posts(slug, 0)

    def unread(self, slug: str, name: str) -> list[Post]:
        """Posts after the seat's cursor, which then moves to the last one; a seat's successor continues from it."""
        held = self.member(slug, name)
        cursor = int(self.redis.hget(f"{self.key(slug)}:read", held) or 0)
        posts = self._posts(slug, cursor)
        if posts:
            self.redis.hset(f"{self.key(slug)}:read", held, cursor + len(posts))
        return posts

    def _posts(self, slug: str, start: int) -> list[Post]:
        return [Post(**json.loads(raw)) for raw in self.redis.lrange(self.key(slug), start, -1)]
