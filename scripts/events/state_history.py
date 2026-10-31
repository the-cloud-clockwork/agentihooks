import argparse
import json
from typing import Any

from .redis_state import HISTORY

TRIM_ONE = """
if redis.call('LLEN', KEYS[1]) <= tonumber(ARGV[1]) then
    return 0
end
redis.call('LTRIM', KEYS[1], 1, -1)
return 1
"""


def trim_page(redis: Any, prefix: str, cursor: int = 0, limit: int = 16) -> tuple[int, int]:
    if not 1 <= limit <= 128 or cursor < 0:
        raise ValueError("limit must be between 1 and 128 and cursor must be nonnegative")
    next_cursor, keys = redis.scan(cursor, match=f"{prefix}:state:*:history:*", count=16)
    removed = 0
    for key in keys:
        for _ in range(limit - removed):
            if not redis.eval(TRIM_ONE, 1, key, HISTORY):
                break
            removed += 1
        if removed == limit:
            return cursor, removed
    return int(next_cursor), removed


def main(argv: list[str] | None = None) -> int:
    from scripts.swarm import store

    parser = argparse.ArgumentParser(
        description="Trim one Redis state history scan page without reading documents",
        epilog="Repeat with the returned cursor. Finish only when both cursor and removed are zero.",
    )
    parser.add_argument("--cursor", type=int, default=0)
    parser.add_argument("--limit", type=int, default=16)
    args = parser.parse_args(argv)
    client = store.redis_client()  # pyright: ignore[reportUnknownMemberType]
    cursor, removed = trim_page(client, store.PREFIX, args.cursor, args.limit)
    print(json.dumps({"cursor": cursor, "removed": removed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
