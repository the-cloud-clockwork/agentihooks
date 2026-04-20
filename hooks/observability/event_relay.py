#!/usr/bin/env python3
"""Event relay hook — publishes Claude transcript events to a Redis Stream.

Self-contained: stdlib + redis package only. No agentihooks/agenticore imports.
Designed to run as a Claude Code hook script via stdin event JSON.

Wired in ~/.claude/settings.json on PostToolUse, Stop, Notification:
  python3 /shared/agentihooks/hooks/observability/event_relay.py

Reads the latest assistant turn from the transcript JSONL file and XADDs each
content block (thinking, tool_use, tool_result, text) to:
  agenticore:events:{AGENTICORE_CORRELATION_ID}

A position cursor at agenticore:pos:eventrelay:{session_id} (or file fallback)
prevents duplicate emission across hook firings within a session.

On Stop, emits a final {event_type: "done"} sentinel and EXPIREs the stream
key in 1h so the SSE consumer (agenticore) terminates its tail loop.

Exit code is always 0 — must never crash the Claude session.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STREAM_KEY_PREFIX = os.environ.get("REDIS_KEY_PREFIX", "agenticore")
STREAM_MAXLEN = 2000
STREAM_TTL_SEC = 3600
POSITION_TTL_SEC = 3600

EVENT_TYPES = {"thinking", "tool_use", "tool_result", "assistant_text", "done"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stream_key(correlation_id: str) -> str:
    return f"{STREAM_KEY_PREFIX}:events:{correlation_id}"


def _position_key(session_id: str) -> str:
    return f"{STREAM_KEY_PREFIX}:pos:eventrelay:{session_id}"


def _position_file(session_id: str) -> Path:
    home = Path(os.environ.get("AGENTIHOOKS_HOME", os.path.expanduser("~/.agentihooks")))
    return home / "event_relay_positions" / f"{session_id}.pos"


def _get_redis():
    """Lazy redis client. Returns None if redis-py absent or REDIS_URL unset."""
    url = os.environ.get("REDIS_URL", "")
    if not url:
        return None
    try:
        import redis

        client = redis.from_url(url, decode_responses=True, socket_timeout=2.0)
        client.ping()
        return client
    except Exception:
        return None


def _load_position(session_id: str) -> int:
    r = _get_redis()
    if r is not None:
        try:
            v = r.get(_position_key(session_id))
            if v is not None:
                return int(v)
        except Exception:
            pass
    f = _position_file(session_id)
    if f.exists():
        try:
            return int(f.read_text().strip())
        except Exception:
            return 0
    return 0


def _save_position(session_id: str, pos: int) -> None:
    r = _get_redis()
    if r is not None:
        try:
            r.setex(_position_key(session_id), POSITION_TTL_SEC, str(pos))
            return
        except Exception:
            pass
    f = _position_file(session_id)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(pos))
    except Exception:
        pass


def extract_events_from_assistant(entry: dict) -> list[dict]:
    """Pull thinking, tool_use, assistant_text events out of an assistant transcript entry."""
    out = []
    content = entry.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "thinking":
            text = block.get("thinking", "")
            if text:
                out.append({"event_type": "thinking", "content": text})
        elif bt == "tool_use":
            out.append(
                {
                    "event_type": "tool_use",
                    "content": json.dumps(
                        {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                        }
                    ),
                }
            )
        elif bt == "text":
            text = block.get("text", "")
            if text:
                out.append({"event_type": "assistant_text", "content": text})
    return out


def extract_events_from_user(entry: dict) -> list[dict]:
    """Pull tool_result events out of a user transcript entry."""
    out = []
    content = entry.get("message", {}).get("content", "")
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_result":
            continue
        raw = block.get("content", "")
        if isinstance(raw, list):
            parts = []
            for item in raw:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            text = "\n".join(parts)
        else:
            text = str(raw)
        out.append(
            {
                "event_type": "tool_result",
                "content": json.dumps(
                    {
                        "tool_use_id": block.get("tool_use_id", ""),
                        "is_error": bool(block.get("is_error", False)),
                        "output": text,
                    }
                ),
            }
        )
    return out


def extract_events_from_entry(entry: dict, drop_tool_events: bool = False) -> list[dict]:
    """Dispatch to assistant/user extractors. Returns [] for other entry types.

    drop_tool_events: when True, skip tool_use and tool_result blocks. Used by
    the Stop reader because PostToolUse already published those — keeping them
    here would double-emit if the JSONL flush race causes the transcript cursor
    to lag behind the actual tool_use writes.
    """
    t = entry.get("type")
    if t == "assistant":
        events = extract_events_from_assistant(entry)
        if drop_tool_events:
            events = [e for e in events if e["event_type"] != "tool_use"]
        return events
    if t == "user":
        if drop_tool_events:
            return []
        return extract_events_from_user(entry)
    return []


def read_new_transcript_events(
    session_id: str,
    transcript_path: str,
    drop_tool_events: bool = False,
) -> tuple[list[dict], int]:
    """Returns (events, new_position). Reads transcript JSONL from byte offset.

    drop_tool_events: pass True from the Stop hook so tool_use/tool_result
    blocks are not re-emitted (PostToolUse already published them).
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return [], 0
    pos = _load_position(session_id)
    events = []
    try:
        with open(transcript_path, "rb") as f:
            f.seek(pos)
            data = f.read()
            new_pos = pos + len(data)
        for raw_line in data.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            events.extend(extract_events_from_entry(entry, drop_tool_events=drop_tool_events))
        return events, new_pos
    except Exception:
        return [], pos


def publish_events(correlation_id: str, session_id: str, events: list[dict]) -> int:
    """XADD events to the per-correlation stream. Returns count published."""
    r = _get_redis()
    if r is None or not events:
        return 0
    key = _stream_key(correlation_id)
    count = 0
    ts = _now_iso()
    for ev in events:
        try:
            r.xadd(
                key,
                {
                    "event_type": ev.get("event_type", "unknown"),
                    "content": ev.get("content", ""),
                    "ts": ts,
                    "session_id": session_id or "",
                    "correlation_id": correlation_id,
                },
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
            count += 1
        except Exception:
            pass
    try:
        r.expire(key, STREAM_TTL_SEC)
    except Exception:
        pass
    return count


def publish_done(correlation_id: str, session_id: str) -> None:
    """Emit a done sentinel + set TTL. Tells the SSE tailer to stop."""
    r = _get_redis()
    if r is None:
        return
    key = _stream_key(correlation_id)
    try:
        r.xadd(
            key,
            {
                "event_type": "done",
                "content": "",
                "ts": _now_iso(),
                "session_id": session_id or "",
                "correlation_id": correlation_id,
            },
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        r.expire(key, STREAM_TTL_SEC)
    except Exception:
        pass


def events_from_post_tool_use(payload: dict) -> list[dict]:
    """Build tool_use + tool_result events directly from a PostToolUse hook payload.

    Field names per Claude Code's actual hook protocol:
      - tool_name, tool_input, tool_use_id (top-level)
      - tool_response (dict or string with the tool's output) — NOT tool_output
      - is_error may be None (older versions) or in tool_response

    We don't depend on the transcript JSONL being flushed yet (a known race
    condition with `claude -p` Stop hooks).
    """
    out = []
    tool_name = payload.get("tool_name", "")
    if not tool_name or tool_name.startswith("system/"):
        return out
    tool_input = payload.get("tool_input", {}) or {}
    tool_use_id = payload.get("tool_use_id", "")
    out.append(
        {
            "event_type": "tool_use",
            "content": json.dumps(
                {
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": tool_input,
                }
            ),
        }
    )

    tool_response = payload.get("tool_response")
    if tool_response is None:
        tool_response = payload.get("tool_output")
    is_error_top = payload.get("is_error")

    if tool_response is None and not is_error_top:
        return out

    if isinstance(tool_response, dict):
        is_error = bool(tool_response.get("is_error", is_error_top or False))
        for key in ("stdout", "output", "result", "content"):
            if key in tool_response and tool_response[key] is not None:
                output_text = str(tool_response[key])
                break
        else:
            output_text = json.dumps(tool_response)
    elif isinstance(tool_response, list):
        output_text = json.dumps(tool_response)
        is_error = bool(is_error_top)
    else:
        output_text = "" if tool_response is None else str(tool_response)
        is_error = bool(is_error_top)

    if not output_text and not is_error:
        return out

    out.append(
        {
            "event_type": "tool_result",
            "content": json.dumps(
                {
                    "tool_use_id": tool_use_id,
                    "is_error": is_error,
                    "output": output_text,
                }
            ),
        }
    )
    return out


def events_from_stop_payload(payload: dict) -> list[dict]:
    """Extract assistant_text from a Stop hook payload's last_assistant_message field.

    Used as fallback when the JSONL transcript hasn't been flushed yet.
    """
    out = []
    last_msg = payload.get("last_assistant_message", "") or ""
    if isinstance(last_msg, str) and last_msg.strip():
        out.append({"event_type": "assistant_text", "content": last_msg})
    return out


def main() -> None:
    correlation_id = os.environ.get("AGENTICORE_CORRELATION_ID", "")
    if not correlation_id:
        sys.exit(0)
    if os.environ.get("AGENTICORE_EVENT_STREAM", "") != "1":
        sys.exit(0)
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        event = json.loads(raw)
    except Exception:
        sys.exit(0)

    hook_name = event.get("hook_event_name", "") or event.get("hook_name", "") or os.environ.get("CLAUDE_HOOK_NAME", "")
    session_id = event.get("session_id", "") or os.environ.get("CLAUDE_SESSION_ID", "")
    transcript_path = event.get("transcript_path", "")

    try:
        all_events: list[dict] = []
        if hook_name == "PostToolUse":
            all_events.extend(events_from_post_tool_use(event))
            _, new_pos = read_new_transcript_events(session_id, transcript_path)
            if new_pos:
                _save_position(session_id, new_pos)
        else:
            drop_tools = hook_name == "Stop"
            events_from_file, new_pos = read_new_transcript_events(
                session_id,
                transcript_path,
                drop_tool_events=drop_tools,
            )
            if events_from_file:
                all_events.extend(events_from_file)
                _save_position(session_id, new_pos)
        if hook_name == "Stop" and not any(e["event_type"] == "assistant_text" for e in all_events):
            all_events.extend(events_from_stop_payload(event))
        if all_events:
            publish_events(correlation_id, session_id, all_events)
        if hook_name == "Stop":
            publish_done(correlation_id, session_id)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
