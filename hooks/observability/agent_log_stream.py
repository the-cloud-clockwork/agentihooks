"""
Stream Claude Code transcript to centralized agent log file.

This module copies transcript entries from Claude Code's session files
to a centralized log file ($AGENTIHOOKS_HOME/logs/agent.log by default) in real-time.

Key features:
- Position tracking to avoid duplicates
- Works with hook payload (session_id + transcript_path)
- Creates single known log file for tailing/monitoring
- No need to find session files manually
"""

import json
from pathlib import Path

from hooks.config import AGENT_LOG_FILE, AGENTIHOOKS_HOME, STREAM_AGENT_LOG
from hooks.logfile import rotate_if_full

# Track last copied position per session
POSITION_DIR = AGENTIHOOKS_HOME / "agent_stream_positions"


def get_last_position(session_id: str) -> int:
    """Get last streamed line number for session.

    Tries Redis first, falls back to file-based position tracking.
    """
    # Try Redis first
    try:
        from hooks._redis import get_redis, redis_key

        r = get_redis()
        if r is not None:
            val = r.get(redis_key("pos:agentstream", session_id))
            if val is not None:
                return int(val)
            return 0
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent fallback

    # File fallback
    POSITION_DIR.mkdir(parents=True, exist_ok=True)
    pos_file = POSITION_DIR / f"{session_id}.pos"
    if pos_file.exists():
        try:
            return int(pos_file.read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def save_position(session_id: str, position: int) -> None:
    """Save last streamed line number.

    Tries Redis first, falls back to file-based position tracking.
    """
    # Try Redis first
    try:
        from hooks._redis import POSITION_TTL, get_redis, redis_key

        r = get_redis()
        if r is not None:
            r.setex(redis_key("pos:agentstream", session_id), POSITION_TTL, str(position))
            return
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent fallback

    # File fallback
    try:
        POSITION_DIR.mkdir(parents=True, exist_ok=True)
        pos_file = POSITION_DIR / f"{session_id}.pos"
        pos_file.write_text(str(position))
    except OSError:
        pass  # Silent failure


def stream_to_agent_log(session_id: str, transcript_path: str) -> None:
    """
    Stream new transcript entries to agent log file.

    Reads the Claude Code transcript file and copies new entries
    to the centralized agent log. Uses position tracking to avoid
    duplicates.

    Args:
        session_id: Conversation session ID (from hook payload)
        transcript_path: Path to Claude Code transcript file (from hook payload)
    """
    if not STREAM_AGENT_LOG:
        return

    source_path = Path(transcript_path)
    if not source_path.exists():
        return

    try:
        # Ensure agent log directory exists
        agent_log = Path(AGENT_LOG_FILE)
        agent_log.parent.mkdir(parents=True, exist_ok=True)

        # Get last processed position
        last_pos = get_last_position(session_id)

        # Read source transcript
        with open(source_path, "r") as f:
            lines = f.readlines()

        # Copy new lines only
        if len(lines) > last_pos:
            new_lines = lines[last_pos:]

            # Append to agent log
            rotate_if_full(agent_log)
            with open(agent_log, "a") as f:
                for line in new_lines:
                    # Validate it's proper JSON before writing
                    try:
                        json.loads(line.strip())
                        f.write(line)
                    except json.JSONDecodeError:
                        pass  # Skip malformed lines

            # Update position
            save_position(session_id, len(lines))

    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent failure - never break Claude


def clear_agent_log() -> None:
    """
    Clear the agent log file.

    Useful for starting fresh or preventing unbounded growth.
    Call this on SessionStart if you want to clear per-session.
    """
    try:
        agent_log = Path(AGENT_LOG_FILE)
        if agent_log.exists():
            agent_log.unlink()
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent failure


def get_agent_log_path() -> str:
    """Get the configured agent log file path."""
    return AGENT_LOG_FILE
