"""Review records: the findings swarm reviewers posted through the forge on one change request head."""

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from typing import Any, NamedTuple, Protocol
from urllib.parse import urlsplit

from scripts.forge.model import Forge
from scripts.gates.base import Who
from scripts.swarm.store import RedisStore, SwarmError

TTL_SEC = 30 * 24 * 3600
OPEN = "open"
READ = "read"
ANSWERED = "answered"


@dataclass(frozen=True)
class Finding:
    id: str
    body: str
    comment: str
    by: str
    task: str
    state: str = OPEN
    answer: str = ""
    read_by: tuple[str, ...] = ()


class Record(NamedTuple):
    url: str
    head: str
    findings: tuple[Finding, ...] = ()


class _Redis(Protocol):
    def hget(self, name: str, key: str) -> str | None: ...

    def transaction(self, update: Callable[[Any], Record], key: str, **kwargs: bool) -> Record: ...


def _redis(store: RedisStore) -> _Redis:
    return store.redis  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def document(found: Record) -> dict[str, Any]:
    return {"url": found.url, "head": found.head, "findings": [asdict(item) for item in found.findings]}


def _key(store: RedisStore, slug: str, url: str) -> str:
    return store.key(slug, "review", sha256(url.encode()).hexdigest()[:16])


def _decode(raw: str | None, url: str, head: str) -> Record:
    if raw is None:
        return Record(url, head)
    data = json.loads(raw)
    return Record(
        data["url"],
        data["head"],
        tuple(
            Finding(
                id=item["id"],
                body=item["body"],
                comment=item["comment"],
                by=item["by"],
                task=item["task"],
                state=item["state"],
                answer=item["answer"],
                read_by=tuple(item["read_by"]),
            )
            for item in data["findings"]
        ),
    )


def read(store: RedisStore, slug: str, url: str, head: str) -> Record | None:
    raw = _redis(store).hget(_key(store, slug, url), head)
    return None if raw is None else _decode(raw, url, head)


def _write(store: RedisStore, slug: str, url: str, head: str, update: Callable[[Record], Record], task: str) -> Record:
    key = _key(store, slug, url)
    tasks = store.key(slug, "review-tasks")

    def change(pipe: Any) -> Record:
        found = update(_decode(pipe.hget(key, head), url, head))
        pipe.multi()
        pipe.hset(key, head, json.dumps(document(found)))
        if task:
            pipe.hincrby(tasks, task, 1)
        pipe.expire(key, TTL_SEC)
        pipe.expire(tasks, TTL_SEC)
        return found

    return _redis(store).transaction(change, key, value_from_callable=True)


def number_of(url: str) -> int:
    try:
        return int(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        raise SwarmError(f"not a change request URL: {url}") from None


def _head(forge: Forge, url: str) -> str:
    found = forge.read(number_of(url))
    if found.url.rstrip("/") != url.rstrip("/"):
        raise SwarmError(f"change request {url} does not match forge target {found.url}")
    return found.head


def _post_one(store: RedisStore, forge: Forge, target: Record, who: Who, body: str) -> Record:
    finding = Finding("", body, forge.post(number_of(target.url), body), who.name, who.task)

    def append(found: Record) -> Record:
        return found._replace(findings=(*found.findings, replace(finding, id=f"f{len(found.findings) + 1}")))

    return _write(store, who.swarm, target.url, target.head, append, who.task)


def _acknowledge(item: Finding, by: str, answer: str) -> Finding:
    return replace(
        item,
        state=ANSWERED if answer else READ,
        answer=answer,
        read_by=tuple(dict.fromkeys((*item.read_by, by))),
    )


def _mark(found: Record, by: str, ids: Sequence[str], answer: str) -> Record:
    if not found.findings:
        raise SwarmError(f"no review on head {found.head} of {found.url}")
    unknown = sorted(set(ids) - {item.id for item in found.findings})
    if unknown:
        raise SwarmError(f"no finding {', '.join(unknown)} on head {found.head} of {found.url}")
    chosen = set(ids) or {item.id for item in found.findings}
    own = [item.id for item in found.findings if item.id in chosen and item.by == by]
    if own:
        raise SwarmError(f"a reviewer cannot mark its own findings read: {', '.join(own)}")
    return found._replace(
        findings=tuple(_acknowledge(item, by, answer) if item.id in chosen else item for item in found.findings)
    )


def mark(store: RedisStore, slug: str, forge: Forge, url: str, by: str, ids: Sequence[str], answer: str) -> Record:
    head = _head(forge, url)
    return _write(store, slug, url, head, lambda found: _mark(found, by, ids, answer), "")


def post(store: RedisStore, slug: str, forge: Forge, url: str, by: str, task: str, bodies: Sequence[str]) -> Record:
    cleaned = tuple(text for body in bodies if (text := body.strip()))
    if not cleaned:
        raise SwarmError("a review posts at least one finding")
    found = Record(url, _head(forge, url))
    who = Who(name=by, swarm=slug, task=task)
    for body in cleaned:
        found = _post_one(store, forge, found, who, body)
    return found
