from typing import Any

from scripts.swarm_v2.accounts import INDEX, INDEX_BUILT, account_key


def listed_by_scan(store: Any) -> list[str]:
    # ruleid: account-keys-scanned
    return sorted(store.redis.scan_iter(match=account_key("*"), count=10000))


def counted_by_scan(store: Any) -> int:
    held = 0
    # ruleid: account-keys-scanned
    for key in store.redis.scan_iter(match=account_key("*")):
        held += len(store.redis.hvals(key))
    return held


def listed_by_keys(store: Any) -> list[str]:
    # ruleid: account-keys-scanned
    return sorted(store.redis.keys(account_key("*")))


def listed_by_index(store: Any) -> list[str]:
    # ok: account-keys-scanned
    return sorted(store.redis.smembers(INDEX))


def _backfill_index(store: Any) -> None:
    # ok: account-keys-scanned
    found = list(store.redis.scan_iter(match=account_key("*"), count=10000))
    store.redis.sadd(INDEX, *found)
    store.redis.set(INDEX_BUILT, 1)
