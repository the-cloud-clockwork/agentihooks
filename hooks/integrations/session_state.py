#!/usr/bin/env python3
"""Session State - Atomic session data enrichment for agenticore.

Provides atomic read/merge/write operations on the shared session map file
(~/conversation_map.json). Any agent or hook can enrich session data which
the API layer reads after session completion.

The session map is keyed by session_id (UUID) and stores arbitrary data:
    {
        "session-uuid-123": {
            "wait": true,
            "platform": "slack",
            "signed_urls": {...},    # Added by hooks
            "custom_field": {...}    # Added by any agent
        }
    }

Usage:
    from hooks.integrations.session_state import enrich_session, get_session

    # Write data to session (atomic merge)
    enrich_session("uuid-123", {
        "signed_urls": {"png": "https://..."},
        "status": "complete"
    })

    # Read session data
    data = get_session("uuid-123")

CLI:
    # Get session data
    session_state.py get --session-id <uuid>

    # Enrich session with JSON data
    session_state.py enrich --session-id <uuid> --data '{"key": "value"}'

    # List all sessions
    session_state.py list
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

# Add repo root to path so hooks package is importable when run as a script
_src_root = Path(__file__).resolve().parent.parent.parent
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

try:
    from hooks.common import log
    from hooks.config import AGENTIHOOKS_HOME
except ImportError:

    def log(msg, ctx=None):
        print(f"[LOG] {msg}: {ctx}", file=sys.stderr)

    import os as _os
    from pathlib import Path as _Path

    AGENTIHOOKS_HOME = _Path(_os.environ.get("AGENTIHOOKS_HOME", str(_Path.home() / ".agentihooks")))


# =============================================================================
# CONFIGURATION
# =============================================================================

# Session map file location (shared with agenticore API)
SESSION_MAP_FILE = AGENTIHOOKS_HOME / "conversation_map.json"


# =============================================================================
# CORE FUNCTIONS
# =============================================================================


def _get_redis_convmap_key(session_id: str) -> str:
    """Build Redis key for conversation map entry."""
    from hooks._redis import redis_key

    return redis_key("convmap", session_id)


def get_session_map() -> Dict[str, Any]:
    """Read the entire session map.

    Returns:
        Dict of all sessions keyed by session_id, or empty dict if file doesn't exist.

    Note: When Redis is active, this only returns file-based sessions.
    Individual session lookups via get_session() check Redis first.
    """
    if not SESSION_MAP_FILE.exists():
        return {}

    try:
        return json.loads(SESSION_MAP_FILE.read_text())
    except (json.JSONDecodeError, IOError) as e:
        log("Failed to read session map", {"error": str(e)})
        return {}


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Get data for a specific session.

    Tries Redis first, falls back to file-based storage.

    Args:
        session_id: The session UUID

    Returns:
        Session data dict or None if not found
    """
    # Try Redis first
    try:
        from hooks._redis import get_redis

        r = get_redis()
        if r is not None:
            raw = r.hgetall(_get_redis_convmap_key(session_id))
            if raw:
                result = {}
                for k, v in raw.items():
                    try:
                        result[k] = json.loads(v)
                    except (json.JSONDecodeError, TypeError):
                        result[k] = v
                return result
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent fallback

    # File fallback
    mappings = get_session_map()
    return mappings.get(session_id)


def enrich_session(session_id: str, data: Dict[str, Any]) -> bool:
    """Atomically merge data into a session entry.

    Tries Redis first (atomic HSET per-field merge), falls back to
    temp file + rename for the file backend.

    Args:
        session_id: The session UUID
        data: Dict of fields to merge into session entry

    Returns:
        True if write succeeded, False otherwise
    """
    # Try Redis first
    try:
        from hooks._redis import SESSION_TTL, get_redis

        r = get_redis()
        if r is not None:
            key = _get_redis_convmap_key(session_id)
            hash_data = {k: json.dumps(v, default=str) if not isinstance(v, str) else v for k, v in data.items()}
            r.hset(key, mapping=hash_data)
            r.expire(key, SESSION_TTL)
            log(
                "Enriched session (Redis)",
                {"session_id": session_id, "keys_added": list(data.keys())},
            )
            return True
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent fallback to file

    # File fallback
    try:
        mappings = get_session_map()

        if session_id not in mappings:
            mappings[session_id] = {}

        mappings[session_id].update(data)

        parent_dir = SESSION_MAP_FILE.parent
        fd, tmp_path = tempfile.mkstemp(dir=parent_dir, suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(mappings, f, indent=2, default=str)
            os.rename(tmp_path, SESSION_MAP_FILE)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        log(
            "Enriched session",
            {
                "session_id": session_id,
                "keys_added": list(data.keys()),
                "file": str(SESSION_MAP_FILE),
            },
        )
        return True

    except Exception as e:
        log("Failed to enrich session", {"error": str(e), "session_id": session_id})
        return False


def delete_session(session_id: str) -> bool:
    """Remove a session entry from the map.

    Tries Redis first, falls back to file-based storage.

    Args:
        session_id: The session UUID to remove

    Returns:
        True if deletion succeeded, False otherwise
    """
    # Try Redis first
    try:
        from hooks._redis import get_redis

        r = get_redis()
        if r is not None:
            key = _get_redis_convmap_key(session_id)
            r.delete(key)
            log("Deleted session (Redis)", {"session_id": session_id})
            return True
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent fallback to file

    # File fallback
    try:
        mappings = get_session_map()

        if session_id not in mappings:
            return True  # Already gone

        del mappings[session_id]

        parent_dir = SESSION_MAP_FILE.parent
        fd, tmp_path = tempfile.mkstemp(dir=parent_dir, suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(mappings, f, indent=2, default=str)
            os.rename(tmp_path, SESSION_MAP_FILE)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        log("Deleted session", {"session_id": session_id})
        return True

    except Exception as e:
        log("Failed to delete session", {"error": str(e), "session_id": session_id})
        return False


# =============================================================================
# CLI INTERFACE
# =============================================================================


def main():
    """CLI entry point for session state operations."""
    if len(sys.argv) < 2:
        print("Usage: session_state.py <command> [args]")
        print("")
        print("Commands:")
        print("  get --session-id <uuid>              Get session data")
        print("  enrich --session-id <uuid> --data {} Merge data into session")
        print("  delete --session-id <uuid>           Remove session entry")
        print("  list                                 List all session IDs")
        print("")
        print("Examples:")
        print('  session_state.py get --session-id "uuid-123"')
        print('  session_state.py enrich --session-id "uuid-123" --data \'{"status": "done"}\'')
        print("  session_state.py list")
        sys.exit(1)

    command = sys.argv[1]

    if command == "get":
        # Parse --session-id
        session_id = None
        args = sys.argv[2:]
        for i, arg in enumerate(args):
            if arg == "--session-id" and i + 1 < len(args):
                session_id = args[i + 1]
                break

        if not session_id:
            print(json.dumps({"error": "Missing --session-id"}))
            sys.exit(1)

        data = get_session(session_id)
        if data is None:
            print(json.dumps({"error": "Session not found", "session_id": session_id}))
            sys.exit(1)

        print(json.dumps(data, indent=2, default=str))
        sys.exit(0)

    elif command == "enrich":
        # Parse --session-id and --data
        session_id = None
        data_str = None
        args = sys.argv[2:]
        i = 0
        while i < len(args):
            if args[i] == "--session-id" and i + 1 < len(args):
                session_id = args[i + 1]
                i += 2
            elif args[i] == "--data" and i + 1 < len(args):
                data_str = args[i + 1]
                i += 2
            else:
                i += 1

        if not session_id:
            print(json.dumps({"error": "Missing --session-id"}))
            sys.exit(1)

        if not data_str:
            print(json.dumps({"error": "Missing --data"}))
            sys.exit(1)

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid JSON: {e}"}))
            sys.exit(1)

        success = enrich_session(session_id, data)
        print(json.dumps({"success": success, "session_id": session_id}))
        sys.exit(0 if success else 1)

    elif command == "delete":
        # Parse --session-id
        session_id = None
        args = sys.argv[2:]
        for i, arg in enumerate(args):
            if arg == "--session-id" and i + 1 < len(args):
                session_id = args[i + 1]
                break

        if not session_id:
            print(json.dumps({"error": "Missing --session-id"}))
            sys.exit(1)

        success = delete_session(session_id)
        print(json.dumps({"success": success, "session_id": session_id}))
        sys.exit(0 if success else 1)

    elif command == "list":
        mappings = get_session_map()
        print(
            json.dumps(
                {
                    "count": len(mappings),
                    "session_ids": list(mappings.keys()),
                },
                indent=2,
            )
        )
        sys.exit(0)

    else:
        print(f"Error: Unknown command '{command}'")
        sys.exit(1)


if __name__ == "__main__":
    main()
