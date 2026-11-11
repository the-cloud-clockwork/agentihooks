"""Paged and keyed Redis hash reads for per pass tick code, which never reads a whole hash in one call."""

from collections.abc import Iterable
from typing import Any

PAGE = 200
PAGES = 1000


def fields(client: Any, key: str, page: int = PAGE) -> dict[Any, Any]:
    """Every field of the hash at key, read with counted hash scans of at most PAGES pages."""
    found: dict[Any, Any] = {}
    cursor = 0
    for _ in range(PAGES):
        cursor, chunk = client.hscan(key, cursor, count=page)
        found.update(chunk)
        if not cursor:
            break
    return found


def picked(client: Any, key: str, names: Iterable[Any]) -> dict[Any, Any]:
    """The named fields of the hash at key that are set, read in one keyed call."""
    wanted = list(dict.fromkeys(names))
    if not wanted:
        return {}
    return {name: value for name, value in zip(wanted, client.hmget(key, wanted), strict=True) if value is not None}
