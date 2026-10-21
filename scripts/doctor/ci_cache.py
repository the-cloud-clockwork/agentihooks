from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import lz4.frame
import orjson

from scripts.swarm.keyspace import ROOT

if TYPE_CHECKING:
    from redis import Redis

PREFIX = f"{ROOT}:doctor:ci-attempt:"
TTL = 3600
ENTRY_BYTES = 8 * 1024 * 1024
TOTAL_BYTES = 512 * 1024 * 1024
VERSION = "l4:"


def encode(attempt: dict) -> str:
    return VERSION + base64.b64encode(lz4.frame.compress(orjson.dumps(attempt))).decode("ascii")


def read(cache: Redis, key: str) -> dict | None:
    saved = cache.get(key)
    if saved is None:
        return None
    if saved.startswith(VERSION):
        return orjson.loads(lz4.frame.decompress(base64.b64decode(saved[len(VERSION) :])))
    attempt = orjson.loads(saved)
    write(cache, key, attempt)
    return attempt


def write(cache: Redis, key: str, attempt: dict) -> None:
    encoded = encode(attempt)
    with cache.lock(f"{PREFIX}lock", timeout=30, blocking_timeout=10):
        if len(encoded) <= ENTRY_BYTES:
            changes = cache.pipeline()
            changes.set(key, encoded, keepttl=True)
            changes.expire(key, TTL, lt=True)
            changes.execute()
        else:
            cache.unlink(key)
        _trim(cache, key)


def _trim(cache: Redis, current: str) -> None:
    keys = sorted(set(cache.scan_iter(match=f"{PREFIX}*", count=1000)) - {f"{PREFIX}lock"})
    keys.sort(key=lambda key: key != current)
    sizes = cache.pipeline(transaction=False)
    for key in keys:
        sizes.strlen(key)
    changes = cache.pipeline(transaction=False)
    total = 0
    for key, size in zip(keys, sizes.execute(), strict=True):
        if size > ENTRY_BYTES or total + size > TOTAL_BYTES:
            changes.unlink(key)
        else:
            total += size
            changes.expire(key, TTL, lt=True)
    changes.execute()
