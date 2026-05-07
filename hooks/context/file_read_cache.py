"""File read deduplication — blocks redundant file re-reads within a session.

Uses Redis as primary storage (Set for paths, Hash for mtimes).
Falls back to an in-process memory dict when Redis is unavailable.

Redis keys:
    agenticore:file_cache:{session_id}  — Set of already-read file paths
    agenticore:file_mtime:{session_id}  — Hash of path → mtime float

The mtime guard ensures that if a file is modified between reads, the second
read is allowed through (and the stored mtime is updated).
"""

import os
from typing import Optional

from hooks._redis import get_redis, redis_key
from hooks.common import log

# In-process fallback when Redis is unavailable
# Structure: {session_id: {file_path: mtime_float}}
_memory_cache: dict[str, dict[str, float]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_mtime(file_path: str) -> Optional[float]:
    """Return file mtime as float, or None if the file cannot be stat'd."""
    try:
        return os.stat(file_path).st_mtime
    except OSError:
        return None


def _cache_key(session_id: str) -> str:
    return redis_key("file_cache", session_id)


def _mtime_key(session_id: str) -> str:
    return redis_key("file_mtime", session_id)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def was_file_read(session_id: str, file_path: str) -> bool:
    """Return True if *file_path* was already read in *session_id* and is unmodified."""
    r = get_redis()

    if r is not None:
        try:
            if not r.sismember(_cache_key(session_id), file_path):
                return False
            # Check mtime
            stored_mtime = r.hget(_mtime_key(session_id), file_path)
            if stored_mtime is None:
                return True  # No mtime stored — assume unchanged
            current_mtime = _get_mtime(file_path)
            if current_mtime is None:
                return True  # Cannot stat — assume unchanged
            return abs(float(stored_mtime) - current_mtime) < 0.001
        except Exception as e:
            log("file_read_cache: Redis read error", {"error": str(e)})
            return False
    else:
        # Memory fallback
        session_map = _memory_cache.get(session_id, {})
        if file_path not in session_map:
            return False
        stored_mtime = session_map[file_path]
        current_mtime = _get_mtime(file_path)
        if current_mtime is None:
            return True
        return abs(stored_mtime - current_mtime) < 0.001


def mark_file_read(session_id: str, file_path: str) -> None:
    """Record that *file_path* was read in *session_id*, storing current mtime."""
    from hooks.config import FILE_READ_CACHE_TTL

    mtime = _get_mtime(file_path)
    if mtime is None:
        mtime = 0.0

    r = get_redis()
    if r is not None:
        try:
            cache_k = _cache_key(session_id)
            mtime_k = _mtime_key(session_id)
            r.sadd(cache_k, file_path)
            r.expire(cache_k, FILE_READ_CACHE_TTL)
            r.hset(mtime_k, file_path, mtime)
            r.expire(mtime_k, FILE_READ_CACHE_TTL)
        except Exception as e:
            log("file_read_cache: Redis write error", {"error": str(e)})
    else:
        # Memory fallback
        if session_id not in _memory_cache:
            _memory_cache[session_id] = {}
        _memory_cache[session_id][file_path] = mtime


def check_and_block_redundant_read(payload: dict) -> None:
    """Raise BlockAction if the file is already in context and unmodified.

    Should be called from on_pre_tool_use for tool_name == "Read".
    """
    from hooks.hook_manager import BlockAction

    session_id = payload.get("session_id", "")
    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "")

    if not session_id or not file_path:
        return

    if was_file_read(session_id, file_path):
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
