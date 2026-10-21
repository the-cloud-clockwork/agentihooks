from __future__ import annotations

import base64
import json
import zlib
from typing import TYPE_CHECKING

from scripts.swarm.keyspace import ROOT

if TYPE_CHECKING:
    from redis import Redis

PREFIX = f"{ROOT}:doctor:ci-attempt:"
TTL = 3600
ENTRY_BYTES = 1024 * 1024
TOTAL_BYTES = 16 * ENTRY_BYTES
VERSION = "z1:"


def encode(attempt: dict) -> str:
    payload = json.dumps(attempt, separators=(",", ":")).encode()
    return VERSION + base64.b64encode(zlib.compress(payload, level=1)).decode("ascii")


def read(cache: Redis, key: str) -> dict | None:
    saved = cache.get(key)
    if saved is None:
        return None
    if saved.startswith(VERSION):
        return json.loads(zlib.decompress(base64.b64decode(saved[len(VERSION) :])))
    attempt = json.loads(saved)
    write(cache, key, attempt)
    return attempt


def write(cache: Redis, key: str, attempt: dict) -> None:
    encoded = encode(attempt)
    with cache.lock(f"{PREFIX}lock", timeout=30, blocking_timeout=10):
        if len(encoded) <= ENTRY_BYTES:
            cache.set(key, encoded, ex=TTL)
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
