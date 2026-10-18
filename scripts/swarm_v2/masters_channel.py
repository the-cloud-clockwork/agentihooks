"""The masters channel of one swarm: only its master seats post and read; posts stay until the swarm is removed."""

import json
from dataclasses import dataclass

from scripts.inbox.seats import SeatRegistry
from scripts.inbox.store import InboxStore
from scripts.swarm.store import PREFIX
from scripts.swarm_v2.masters import MasterError, MasterSeats, seats

SWARM = "swarm"


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
        """Every other occupied master seat gets an inbox item naming the post; the text stays in the channel."""
        if not text.strip():
            raise MasterError("a masters channel post needs text")
        posted = self._append(slug, self.member(slug, name), name, text, at)
        notice = (
            f"masters channel post {posted.number} from {posted.seat}: "
            f"read it with agentihooks swarm {slug} masters-channel read"
        )
        registry, inbox = SeatRegistry(self.redis), InboxStore(self.redis)
        for seat in seats(slug, MasterSeats(self.redis).count(slug)):
            if seat != posted.seat and registry.occupant(seat).occupant:
                inbox.send(name, seat, notice, fyi=True)
        return posted

    def announce(self, slug: str, text: str, at: float) -> Post:
        """A post by the swarm tick itself, which holds no seat."""
        return self._append(slug, SWARM, SWARM, text, at)

    def decision(self, slug: str, seat: str, by: str, text: str, at: float) -> Post:
        """A decision the master fence already admitted for this seat."""
        return self._append(slug, seat, by, text, at)

    def _append(self, slug: str, seat: str, by: str, text: str, at: float) -> Post:
        fields = {"seat": seat, "by": by, "text": text, "at": at}
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
