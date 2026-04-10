"""Image Persistence Reminder — re-injects the live-patch → image-rebuild rule.

Every N tool calls in a session, inject a reminder that any validated live
patch MUST be promoted to an image rebuild+push. Prevents drift in long
sessions where the operator is doing live iteration on pods/containers.

Counter is per-session, Redis-backed with in-memory fallback.

Config (env vars, read via hooks.config):
    IMAGE_PERSISTENCE_REMINDER_ENABLED (bool, default True)
    IMAGE_PERSISTENCE_REMINDER_INTERVAL (int, default 10)

Public API:
    on_post_tool_result(payload) -> str | None
        Returns the reminder text to inject, or None if not due yet.
    clear_session_state(session_id)
"""

from hooks._redis import get_redis, redis_key
from hooks.common import log

_memory_counter: dict[str, int] = {}

REMINDER_TEXT = (
    "[image-persistence reminder] Live-patch → image-rebuild is a HARD RULE. "
    "If any live patch (kubectl exec/cp, SSH edit on a running container, in-pod config change) "
    "has been validated in this session and you have NOT yet kicked off an image rebuild+push "
    "carrying that change, do it NOW in parallel — local `docker build`+`docker push` or CI, "
    "whichever is less intrusive. Set a CronCreate poll to track completion. A live patch that "
    "isn't baked into an image is one pod restart away from being lost. "
    "See ~/.claude/rules/operator-image-persistence.md."
)


def _get_counter(session_id: str) -> int:
    r = get_redis()
    if r is not None:
        try:
            key = redis_key("image_persistence_reminder", session_id)
            val = r.get(key)
            return int(val) if val else 0
        except Exception as e:
            log("image_persistence_reminder redis get failed", {"error": str(e)})
    return _memory_counter.get(session_id, 0)


def _set_counter(session_id: str, value: int) -> None:
    r = get_redis()
    if r is not None:
        try:
            key = redis_key("image_persistence_reminder", session_id)
            r.set(key, value, ex=86400)  # 24h TTL
            return
        except Exception as e:
            log("image_persistence_reminder redis set failed", {"error": str(e)})
    _memory_counter[session_id] = value


def on_post_tool_result(payload: dict) -> str | None:
    """Increment per-session counter; return reminder text when interval hit."""
    try:
        from hooks.config import (
            IMAGE_PERSISTENCE_REMINDER_ENABLED,
            IMAGE_PERSISTENCE_REMINDER_INTERVAL,
        )
    except ImportError:
        return None

    if not IMAGE_PERSISTENCE_REMINDER_ENABLED:
        return None

    session_id = payload.get("session_id", "")
    if not session_id:
        return None

    interval = max(1, int(IMAGE_PERSISTENCE_REMINDER_INTERVAL))
    count = _get_counter(session_id) + 1
    _set_counter(session_id, count)

    if count % interval == 0:
        log("image_persistence_reminder injected", {"session_id": session_id, "count": count})
        return REMINDER_TEXT
    return None


def clear_session_state(session_id: str) -> None:
    r = get_redis()
    if r is not None:
        try:
            r.delete(redis_key("image_persistence_reminder", session_id))
        except Exception:
            pass
    _memory_counter.pop(session_id, None)
