from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from scripts.doctor.priming import doctor_slug
from scripts.swarm import naming
from scripts.swarm.store import PREFIX, RedisStore

if TYPE_CHECKING:
    from redis import Redis


def repository(repo: str, run: Callable = subprocess.run) -> str:
    try:
        result = run(
            ["git", "-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return str(Path(repo).resolve())
    return str(Path(result.stdout.strip()).resolve()) if result.returncode == 0 else str(Path(repo).resolve())


def key(repo: str) -> str:
    identity = sha256(repository(repo).encode()).hexdigest()
    return f"{PREFIX}:promotion-owner:{identity}"


def select(slug: str, repo: str, store: RedisStore) -> str:
    candidates = {
        name: config
        for name in store.slugs()
        if not naming.PROOF_RE.fullmatch(name)
        and (config := store.config(name)).state == "running"
        and repository(config.repo) == repository(repo)
    }
    record = key(repo)
    previous = store.redis.hget(record, "owner")
    if previous in candidates:
        return previous
    preferred = [name for name in candidates if not (peer := store.peer(name)) or doctor_slug(peer) != name]
    owner = min(preferred or candidates or [slug])
    store.redis.hset(record, mapping={"owner": owner, "previous": previous or "", f"swarm:{owner}": owner})
    return owner


def history(repo: str, redis: Redis) -> set[str]:
    return {value for field, value in redis.hgetall(key(repo)).items() if field.startswith("swarm:")}


@contextmanager
def intake(slug: str, repo: str, store: RedisStore) -> Iterator[str | None]:
    lock, token = f"{key(repo)}:lock", uuid4().hex
    if not store.redis.set(lock, token, nx=True, ex=300):
        yield None
        return
    try:
        yield select(slug, repo, store)
    finally:
        if store.redis.get(lock) == token:
            store.redis.delete(lock)
