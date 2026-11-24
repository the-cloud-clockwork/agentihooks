from types import SimpleNamespace
from typing import cast

from scripts.forge.model import ChangeRequest
from scripts.swarm.store import RedisStore

SLUG = "sw"
URL = "https://github.com/o/r/pull/7"
HEAD = "abc123def456"
REVIEWER = "reviewer@a1b2c3-0002"


class FakeForge:
    """A forge that reports one head for the change request and answers each post with a numbered comment URL."""

    def __init__(self, head: str = HEAD, url: str = URL) -> None:
        self.head = head
        self.url = url
        self.reads: list[int] = []
        self.posts: list[tuple[int, str]] = []

    def read(self, number: int) -> ChangeRequest:
        self.reads.append(number)
        return ChangeRequest(number, self.url, "Title", "Body", self.head, "", (), ())

    def post(self, number: int, body: str) -> str:
        self.posts.append((number, body))
        return f"{URL}#issuecomment-{len(self.posts)}"


def store() -> RedisStore:
    """Return a swarm store on an in-memory Redis."""
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def swarm(name: str = REVIEWER, task: str = "r1") -> RedisStore:
    """Return a swarm store on an in-memory Redis whose only agent is the named one."""
    found = store()
    agent = SimpleNamespace(name=name, lane="review", task=task)
    shaped = SimpleNamespace(
        names=SimpleNamespace(resolve=lambda name: name),
        agents=lambda slug: [agent],
        redis=found.redis,
        key=found.key,
    )
    return cast(RedisStore, shaped)
