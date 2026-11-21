"""The review gate: a change request head queues only after an independent review whose findings its author read."""

from scripts.review.record import Record
from scripts.swarm.store import AgentRecord, RedisStore


def refusal(found: Record | None, slug: str, url: str, head: str, author: AgentRecord) -> str | None:
    raise NotImplementedError


def admit(store: RedisStore, slug: str, agent: AgentRecord, url: str, head: str) -> None:
    return None
