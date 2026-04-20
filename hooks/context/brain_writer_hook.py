"""Brain writer hook — scans session transcript for brain markers, routes them.

Called from on_stop() in hook_manager.py. Reads the full session transcript,
extracts assistant response text, parses HTML comment markers, then routes:
  - lesson, decision  → local outbox (staged for vault write via rsync)
  - milestone, signal → local outbox + Redis XADD to anton:events:brain

Outbox format: ~/.agentihooks/brain-outbox/<timestamp>-<type>-<uuid>.json
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hooks.common import log

# ── Inline marker regex (same patterns as brain-tools/markers.py) ────

_BLOCK_RE = re.compile(
    r"<!--\s*@(\w+)((?:\s+\w+=[^\s>]+|\s+\w+=\"[^\"]*\")*)\s*-->"
    r"(.*?)"
    r"<!--\s*@/\1\s*-->",
    re.DOTALL,
)
_ATTR_RE = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')
_WRITER_TYPES = frozenset({"lesson", "milestone", "signal", "decision"})


def _parse_attrs(attr_str: str) -> dict[str, str]:
    return {m.group(1): m.group(2) or m.group(3) for m in _ATTR_RE.finditer(attr_str)}


def _find_markers(text: str) -> list[dict[str, Any]]:
    """Extract brain markers from raw text. Returns list of marker dicts."""
    results = []
    for m in _BLOCK_RE.finditer(text):
        mtype = m.group(1).lower()
        if mtype not in _WRITER_TYPES:
            continue
        attrs = _parse_attrs(m.group(2))
        content = m.group(3).strip()
        if content:
            results.append({"type": mtype, "attrs": attrs, "content": content})
    return results


# ── Transcript parsing ───────────────────────────────────────────────


def _parse_transcript_for_markers(transcript_path: str, max_markers: int) -> list[dict]:
    """Read JSONL transcript, extract markers from assistant responses."""
    path = Path(transcript_path)
    if not path.exists():
        return []

    all_text: list[str] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        for block in entry.get("message", {}).get("content", []):
            if block.get("type") == "text":
                all_text.append(block["text"])

    if not all_text:
        return []

    combined = "\n".join(all_text)
    markers = _find_markers(combined)
    return markers[:max_markers]


# ── Outbox write ─────────────────────────────────────────────────────


def _write_to_outbox(markers: list[dict], session_id: str, outbox_dir: str) -> int:
    """Write markers as individual JSON files to the outbox directory."""
    outbox = Path(outbox_dir)
    outbox.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    count = 0
    for marker in markers:
        ts = now.strftime("%Y%m%dT%H%M%S")
        uid = uuid.uuid4().hex[:8]
        filename = f"{ts}-{marker['type']}-{uid}.json"
        payload = {
            "type": marker["type"],
            "content": marker["content"],
            "attrs": marker["attrs"],
            "session_id": session_id,
            "agent_name": os.getenv("AGENTICORE_AGENT_NAME", os.getenv("USER", "unknown")),
            "project": os.getenv("CLAUDE_PROJECT_DIR", ""),
            "ts": now.isoformat(),
        }
        (outbox / filename).write_text(json.dumps(payload, indent=2))
        count += 1
    return count


# ── Redis publish ────────────────────────────────────────────────────


def _publish_to_redis(markers: list[dict], redis_url: str, ssh_key: str) -> int:
    """Publish milestone/signal markers to Redis event bus via SSH + redis-cli."""
    if not redis_url:
        return 0

    # Only milestone and signal go to Redis
    publishable = [m for m in markers if m["type"] in ("milestone", "signal")]
    if not publishable:
        return 0

    # Parse Redis URL
    parsed = urlparse(redis_url)
    host = parsed.hostname or "10.10.30.130"
    password = parsed.password or ""
    db = parsed.path.lstrip("/") or "11"

    count = 0
    for marker in publishable:
        severity = marker["attrs"].get("severity", "info")
        source = marker["attrs"].get("source", "brain-writer")
        scope = marker["attrs"].get("scope", "")
        priority = "urgent" if severity == "nuclear" else "high" if severity == "critical" else "default"

        # Build XADD command
        fields = (
            f"event brain.{marker['type']} "
            f"title '{marker['content'][:80].replace(chr(39), '')}' "
            f"message '{marker['content'][:500].replace(chr(39), '')}' "
            f"source {source} "
            f"priority {priority} "
            f"severity {severity} "
            f"scope {scope} "
            f"ts {int(datetime.now(timezone.utc).timestamp())}"
        )
        cmd = f"docker exec dataplane_redis redis-cli -a {password} -n {db} XADD anton:events:brain '*' {fields}"

        try:
            result = subprocess.run(
                ["ssh", "-i", ssh_key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", f"root@{host}", cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                count += 1
            else:
                log("brain_writer: redis publish failed", {"error": result.stderr[:200]})
        except Exception as e:
            log("brain_writer: redis publish error", {"error": str(e)})

    return count


# ── Main entry point ─────────────────────────────────────────────────


def write_markers(session_id: str, transcript_path: str, last_message: str = "") -> dict:
    """Scan transcript for brain markers, write to outbox, publish to Redis.

    Args:
        last_message: The last assistant message from the Stop payload.
            Used as fallback when the JSONL transcript hasn't been flushed yet
            (race condition in -p mode).
    """
    from hooks.config import (
        BRAIN_WRITER_ENABLED,
        BRAIN_WRITER_MAX_MARKERS,
        BRAIN_WRITER_OUTBOX,
        BRAIN_WRITER_REDIS_URL,
        BRAIN_WRITER_SSH_KEY,
    )

    if not BRAIN_WRITER_ENABLED:
        return {"markers": 0, "reason": "disabled"}

    from hooks.telemetry import span_ctx

    with span_ctx(
        "brain.marker_write",
        {
            "session_id": session_id,
            "transcript_path": transcript_path or "<fallback>",
            "source": "transcript" if transcript_path else "last_message",
        },
    ) as span:
        markers = _parse_transcript_for_markers(transcript_path, BRAIN_WRITER_MAX_MARKERS)

        # Fallback: if transcript had no markers but last_message does, parse that
        if not markers and last_message:
            markers = _find_markers(last_message)[:BRAIN_WRITER_MAX_MARKERS]
        if not markers:
            span.set_attrs({"markers_found": 0})
            return {"markers": 0}

        outbox_count = _write_to_outbox(markers, session_id, BRAIN_WRITER_OUTBOX)
        redis_count = _publish_to_redis(markers, BRAIN_WRITER_REDIS_URL, BRAIN_WRITER_SSH_KEY)

        span.set_attrs(
            {
                "markers_found": len(markers),
                "outbox_count": outbox_count,
                "redis_count": redis_count,
                "marker_types": ",".join(m["type"] for m in markers),
            }
        )
        return {
            "markers": len(markers),
            "outbox": outbox_count,
            "redis": redis_count,
            "types": [m["type"] for m in markers],
        }
