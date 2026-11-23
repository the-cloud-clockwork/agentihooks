"""Review records: the findings swarm reviewers posted through the forge on one change request head."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from scripts.forge.model import Forge
from scripts.swarm.store import RedisStore

TTL_SEC = 0


@dataclass(frozen=True)
class Finding:
    id: str
    body: str
    comment: str
    by: str
    task: str


class Record(NamedTuple):
    url: str
    head: str
    findings: tuple[Finding, ...] = ()


def document(found: Record) -> dict[str, Any]:
    raise NotImplementedError(found)


def read(store: RedisStore, slug: str, url: str, head: str) -> Record | None:
    raise NotImplementedError(store, slug, url, head)


def number_of(url: str) -> int:
    raise NotImplementedError(url)


def mark(store: RedisStore, slug: str, forge: Forge, url: str, by: str, ids: Sequence[str], answer: str) -> Record:
    raise NotImplementedError(store, slug, forge, url, by, ids, answer)


def post(store: RedisStore, slug: str, forge: Forge, url: str, by: str, task: str, bodies: Sequence[str]) -> Record:
    raise NotImplementedError(store, slug, forge, url, by, task, bodies)
