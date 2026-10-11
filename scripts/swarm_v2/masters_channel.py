"""The masters channel of one swarm: only its master seats post and read; posts stay until the swarm is removed."""

import json
from dataclasses import dataclass

from scripts.inbox.seats import SeatRegistry
from scripts.swarm.store import PREFIX
from scripts.swarm_v2.masters import MasterError, MasterSeats, seats


@dataclass(frozen=True)
class Post:
    number: int
    seat: str
    by: str
    text: str
    at: float


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
        held = self.member(slug, name)
        fields = {"seat": held, "by": name, "text": text, "at": at}
        return Post(self.redis.rpush(self.key(slug), json.dumps(fields)), **fields)

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
        raws = self.redis.lrange(self.key(slug), start, -1)
        return [Post(number, **json.loads(raw)) for number, raw in enumerate(raws, start + 1)]
