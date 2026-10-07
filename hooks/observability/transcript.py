import hashlib

"""Automatic transcript logging - logs new entries on each PostToolUse.

Deliberately NOT migrated to hooks.memory.transcript_reader: this is an
incremental line-offset tail (last-logged-line tracking per session), while
the unified reader is a whole-file record parser. Same reasoning applies to
hooks/observability/event_relay.py (binary tail-read). Claude-format only;
codex sessions simply log nothing here.
"""

import json
from pathlib import Path

from hooks.config import AGENTIHOOKS_HOME, LOG_TRANSCRIPT

# Track last logged line per session to avoid duplicates
POSITION_DIR = AGENTIHOOKS_HOME / "transcript_positions"


def get_last_position(session_id: str) -> int:
    """Get last logged line number for session.

    Tries Redis first, falls back to file-based position tracking.
    """
    # Try Redis first
    try:
        from hooks._redis import get_redis, redis_key

        r = get_redis()
        if r is not None:
            val = r.get(redis_key("pos:transcript", session_id))
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
    """Save last logged line number.

    Tries Redis first, falls back to file-based position tracking.
    """
    # Try Redis first
    try:
        from hooks._redis import POSITION_TTL, get_redis, redis_key

        r = get_redis()
        if r is not None:
            r.setex(redis_key("pos:transcript", session_id), POSITION_TTL, str(position))
            return
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent fallback

    # File fallback
    try:
        POSITION_DIR.mkdir(parents=True, exist_ok=True)
        pos_file = POSITION_DIR / f"{session_id}.pos"
        pos_file.write_text(str(position))
    except OSError:
        pass  # Silent failure - never break Claude


def log_new_entries(session_id: str, transcript_path: str) -> None:
    """Read transcript and log any new entries since last check."""
    if not LOG_TRANSCRIPT:
        return

    # Import here to avoid circular imports
    from hooks.common import log_transcript

    path = Path(transcript_path)
    if not path.exists():
        return

    try:
        last_pos = get_last_position(session_id)

        with open(path, "r") as f:
            lines = f.readlines()

        # Log new lines only
        for line in lines[last_pos:]:
            try:
                entry = json.loads(line.strip())
                if isinstance(entry, dict):
                    entry_type = entry.get("type", "unknown")
                    # Only log user and assistant entries
                    if entry_type in ("user", "assistant"):
                        content = extract_content(entry)
                        if content:
                            log_transcript(session_id, entry_type, content)
            except json.JSONDecodeError:
                continue

        # Save new position
        save_position(session_id, len(lines))

    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent failure - never break Claude


def extract_content(entry: dict) -> str | None:
    """Extract text content from transcript entry."""
    message = entry.get("message")

    # Handle message as string
    if isinstance(message, str):
        return message

    # Handle message as dict with content array
    if isinstance(message, dict):
        content = message.get("content", [])
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        texts.append(text)
            if texts:
                return "\n".join(texts)
        elif isinstance(content, str):
            return content

    return None


def record_id(record: dict) -> str:
    native = record.get("uuid")
    if native:
        return str(native)
    identity = record
    if record.get("type") == "response_item":
        item = record.get("payload", {})
        native = item.get("id") or item.get("call_id")
        if native:
            identity = {"type": record["type"], "kind": item.get("type"), "id": native}
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def complete_records(transcript_path: str) -> tuple[list[dict], int, int]:
    records = []
    position = 0
    unsupported = 0
    with open(transcript_path, "rb") as handle:
        for line in handle:
            if not line.endswith(b"\n"):
                break
            position += len(line)
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                unsupported += 1
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                unsupported += 1
    return records, position, unsupported


def mask_value(value: object) -> object:
    from hooks.secrets import redact

    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                return redact(value, mode="strict")
            masked = mask_value(parsed)
            if masked != parsed:
                return json.dumps(masked, ensure_ascii=False)
        return redact(value, mode="strict")
    if isinstance(value, (list, tuple)):
        return [mask_value(item) for item in value]
    if isinstance(value, dict):
        return {redact(key, mode="strict"): mask_member(key, item) for key, item in value.items()}
    return value


def mask_member(key: str, value: object) -> object:
    from hooks.secrets import redact

    if isinstance(value, (list, tuple)):
        return [mask_member(key, item) for item in value]
    if isinstance(value, dict):
        probe = f"{key}=12345678"
        redacted = redact(probe, mode="standard")
        context = key if redacted.count("[REDACTED:generic_secret]") > probe.count("[REDACTED:generic_secret]") else ""
        return {redact(name, mode="strict"): mask_member(context or name, item) for name, item in value.items()}
    masked = mask_value(value)
    contextual = f"{key}={json.dumps(masked, ensure_ascii=False)}"
    redacted = redact(contextual, mode="standard")
    if redacted.count("[REDACTED:generic_secret]") > contextual.count("[REDACTED:generic_secret]"):
        return "[REDACTED:generic_secret]"
    return masked
