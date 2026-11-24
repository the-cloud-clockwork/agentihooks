"""Review records: the findings swarm reviewers posted through the forge on one change request head."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
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


def _stored(item: Finding) -> dict[str, Any]:
    return {
        "id": item.id,
        "body": item.body,
        "comment": item.comment,
        "by": item.by,
        "task": item.task,
        "state": item.state,
        "answer": item.answer,
        "read_by": list(item.read_by),
    }


def document(found: Record) -> dict[str, Any]:
    return {"url": found.url, "head": found.head, "findings": [_stored(item) for item in found.findings]}


def _key(store: RedisStore, slug: str, url: str) -> str:
    return store.key(slug, "review", sha256(url.rstrip("/").encode()).hexdigest()[:16])


def _decode(raw: str) -> Record:
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
    return None if raw is None else _decode(raw)


def _write(store: RedisStore, slug: str, url: str, head: str, update: Callable[[Record], Record], task: str) -> Record:
    key = _key(store, slug, url)
    tasks = store.key(slug, "review-tasks")

    def change(pipe: Any) -> Record:
        stored = pipe.hget(key, head)
        found = update(Record(url, head) if stored is None else _decode(stored))
        pipe.multi()
        pipe.hset(key, head, json.dumps(document(found)))
        if task:
            pipe.hincrby(tasks, task)
        pipe.expire(key, TTL_SEC)
        pipe.expire(tasks, TTL_SEC)
        return found

    return _redis(store).transaction(change, key, value_from_callable=True)


def number_of(url: str) -> int:
    try:
        return int(urlsplit(url).path.rstrip("/").rpartition("/")[2])
    except ValueError:
        raise SwarmError(f"not a change request URL: {url}") from None


def _head(forge: Forge, url: str) -> str:
    found = forge.read(number_of(url))
    if found.url.rstrip("/") != url.rstrip("/"):
        raise SwarmError(f"change request {url} does not match forge target {found.url}")
    return found.head


def _post_one(store: RedisStore, forge: Forge, target: Record, who: Who, body: str) -> Record:
    comment = forge.post(number_of(target.url), body)

    def append(found: Record) -> Record:
        finding = Finding(f"f{len(found.findings) + 1}", body, comment, who.name, who.task)
        return found._replace(findings=(*found.findings, finding))

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


def done_refusal(store: RedisStore, slug: str, kind: str, lane: str, task: str) -> str | None:
    if "review" not in (kind, lane):
        return None
    if int(_redis(store).hget(store.key(slug, "review-tasks"), task) or 0) > 0:
        return None
    return (
        "a review task is done only after it posts at least one finding: "
        f"agentihooks review post <url> --slug {slug} --finding <text>"
    )


def require_done(store: RedisStore, slug: str, kind: str, lane: str, task: str) -> None:
    if reason := done_refusal(store, slug, kind, lane, task):
        raise SwarmError(reason)
