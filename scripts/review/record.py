"""Review records: the findings independent reviewers posted on one change request head and what its author read."""

from collections.abc import Sequence
from dataclasses import dataclass

from scripts.forge.model import Forge
from scripts.swarm.store import RedisStore

TTL_SEC = 30 * 24 * 3600
OPEN = "open"
READ = "read"
ANSWERED = "answered"
REVIEW_KIND = "review"


@dataclass(frozen=True)
class Finding:
    id: str
    body: str
    comment: str
    by: str
    task: str
    state: str = OPEN
    answer: str = ""


@dataclass(frozen=True)
class Record:
    url: str
    head: str
    findings: tuple[Finding, ...] = ()

    def unread(self) -> tuple[Finding, ...]:
        raise NotImplementedError

    def independent(self, author: str, task: str) -> bool:
        raise NotImplementedError


def read(store: RedisStore, slug: str, url: str, head: str) -> Record | None:
    raise NotImplementedError


def post(store: RedisStore, slug: str, forge: Forge, url: str, by: str, task: str, bodies: Sequence[str]) -> Record:
    raise NotImplementedError


def mark(store: RedisStore, slug: str, forge: Forge, url: str, by: str, ids: Sequence[str], answer: str) -> Record:
    raise NotImplementedError


def done_refusal(store: RedisStore, slug: str, kind: str, task: str) -> str | None:
    raise NotImplementedError
