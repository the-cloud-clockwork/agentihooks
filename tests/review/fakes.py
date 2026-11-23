from types import SimpleNamespace
from typing import cast

from scripts.forge.model import ChangeRequest
from scripts.swarm.store import RedisStore

FEATURE = "review-findings-queue-gate"
SLUG = "sw"
URL = "https://github.com/o/r/pull/7"
HEAD = "abc123def456"
AUTHOR = "engineer@a1b2c3-0001"
REVIEWER = "reviewer@a1b2c3-0002"


class FakeForge:
    """A forge that reports one head for the change request and answers each post with a numbered comment URL."""

    def __init__(self, head: str = HEAD) -> None:
        self.head = head
        self.reads: list[int] = []
        self.posts: list[tuple[int, str]] = []

    def read(self, number: int) -> ChangeRequest:
        self.reads.append(number)
        return ChangeRequest(number, URL, "Title", "Body", self.head, "", (), ())

    def post(self, number: int, body: str) -> str:
        self.posts.append((number, body))
        return f"{URL}#issuecomment-{len(self.posts)}"


def store() -> RedisStore:
    """Return a swarm store on an in-memory Redis."""
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def swarm(gates: dict[str, str], name: str = AUTHOR, task: str = "t1") -> RedisStore:
    """Return a swarm store whose only agent is the named one and whose config carries the gate modes."""
    found = store()
    agent = SimpleNamespace(name=name, lane="eng", task=task)
    shaped = SimpleNamespace(
        names=SimpleNamespace(swarm_slug=lambda slug: slug, resolve=lambda name: name),
        agents=lambda slug: [agent],
        redis=found.redis,
        key=found.key,
        config=lambda slug: SimpleNamespace(gates=gates),
    )
    return cast(RedisStore, shaped)
