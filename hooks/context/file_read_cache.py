"""File read deduplication — blocks redundant file re-reads within a session.

Uses Redis as primary storage (Set for paths, Hash for mtimes).
Falls back to an in-process memory dict when Redis is unavailable.

Redis keys:
    agenticore:file_cache:{session_id}  — Set of already-read file paths
    agenticore:file_mtime:{session_id}  — Hash of path → mtime float

The mtime guard ensures that if a file is modified between reads, the second
read is allowed through (and the stored mtime is updated).
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

from hooks._redis import get_redis, redis_key
from hooks.common import log

# In-process fallback when Redis is unavailable
# Structure: {session_id: {file_path: mtime_float}}
_memory_cache: dict[str, dict[str, tuple[float, list[tuple[int, int]]]]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_mtime(file_path: str) -> float | None:
    """Return the file modification time when it is available."""
    try:
        return Path(file_path).stat().st_mtime
    except OSError:
        return None


def _cache_key(session_id: str) -> str:
    return redis_key("file_read_ranges", session_id)


def _mtime_key(session_id: str) -> str:
    return redis_key("file_mtime", session_id)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _read_ranges(session_id: str, file_path: str) -> list[tuple[int, int]]:
    current_mtime = _get_mtime(file_path)
    r = get_redis()
    if r is not None:
        try:
            stored_mtime = r.hget(_mtime_key(session_id), file_path)
            if stored_mtime is None or current_mtime is None:
                return []
            if abs(float(stored_mtime) - current_mtime) >= 0.001:
                return []
            encoded = r.hget(_cache_key(session_id), file_path)
            return [(pair[0], pair[1]) for pair in json.loads(encoded)] if encoded else []
        except Exception as e:
            log("file_read_cache: Redis read error", {"error": str(e)})
            return []
    stored = _memory_cache.get(session_id, {}).get(file_path)
    if stored is None or current_mtime is None:
        return []
    mtime, ranges = stored
    return ranges if abs(mtime - current_mtime) < 0.001 else []


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def was_file_read(session_id: str, file_path: str, offset: int = 1, limit: int | None = None) -> bool:
    """Return whether every requested line was delivered for this file revision."""
    end = offset + limit - 1 if limit is not None else sys.maxsize
    return any(start <= offset and stop >= end for start, stop in _read_ranges(session_id, file_path))


def mark_file_read(session_id: str, file_path: str, start: int = 1, end: int = sys.maxsize) -> None:
    """Record delivered lines, retaining coverage only for the current revision."""
    from hooks.config import FILE_READ_CACHE_TTL

    mtime = _get_mtime(file_path)
    if mtime is None:
        return
    ranges = _merge_ranges([*_read_ranges(session_id, file_path), (start, end)])
    r = get_redis()
    if r is not None:
        try:
            cache_k = _cache_key(session_id)
            mtime_k = _mtime_key(session_id)
            r.hset(cache_k, file_path, json.dumps(ranges))
            r.expire(cache_k, FILE_READ_CACHE_TTL)
            r.hset(mtime_k, file_path, mtime)
            r.expire(mtime_k, FILE_READ_CACHE_TTL)
        except Exception as e:
            log("file_read_cache: Redis write error", {"error": str(e)})
    else:
        _memory_cache.setdefault(session_id, {})[file_path] = (mtime, ranges)


def _delivered_ranges(response: Any) -> list[tuple[int, int]]:
    if isinstance(response, dict):
        file = response.get("file", {})
        start = file.get("startLine")
        count = file.get("numLines")
        total = file.get("totalLines")
        if isinstance(start, int) and isinstance(count, int) and start > 0 and count > 0:
            end = start + count - 1
            if isinstance(total, int) and end == total:
                end = sys.maxsize
            return [(start, end)]
        return []
    if isinstance(response, str):
        lines = [int(number) for number in re.findall(r"^\s*(\d+)→", response, re.MULTILINE)]
        return _merge_ranges([(number, number) for number in lines])
    return []


def mark_read_result(payload: dict[str, Any]) -> None:
    """Record only successful Read output whose delivered lines are known."""
    response = payload.get("tool_response", payload.get("tool_output"))
    if payload.get("is_error") or isinstance(response, dict) and response.get("is_error"):
        return
    session_id = payload.get("session_id", "")
    file_path = payload.get("tool_input", {}).get("file_path", "")
    if not session_id or not file_path:
        return
    for start, end in _delivered_ranges(response):
        mark_file_read(session_id, file_path, start, end)


def check_and_block_redundant_read(payload: dict[str, Any]) -> None:
    """Refuse a Read only when all requested lines are already in context."""
    from hooks.hook_manager import BlockAction

    session_id = payload.get("session_id", "")
    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "")
    if not session_id or not file_path:
        return
    if was_file_read(session_id, file_path, tool_input.get("offset", 1), tool_input.get("limit")):
        raise BlockAction(
            f"File {file_path} is already in context from this session. "
            "Use the content already available rather than re-reading."
        )


def clear_session_cache(session_id: str) -> None:
    """Delete all cache entries for *session_id* from Redis and memory."""
    try:
        r = get_redis()
        if r is not None:
            try:
                r.delete(_cache_key(session_id), _mtime_key(session_id))
            except Exception as e:
                log("file_read_cache: Redis delete error", {"error": str(e)})
    except KeyboardInterrupt:
        # Operator pressed Ctrl+C mid-shutdown — exit silently.
        pass

    _memory_cache.pop(session_id, None)
