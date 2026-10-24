"""Transcript reading: agent.log search plus the unified transcript-record API.

Two responsibilities share this module because they are the same shape of
problem — "parse a JSONL conversation log into a usable record stream":

1. agent.log search/retrieval (the original content, below).
2. ``iter_transcript_records`` — ONE parser for the ``transcript_path`` a hook
   payload points at, covering both host formats:
   - Claude Code transcripts: entries ``{type: user|assistant, message:
     {content: [{type: text|tool_use|tool_result, ...}]}}`` (JSONL or a bare
     JSON array).
   - Codex rollouts (verified against codex-cli 0.147.0): lines ``{type,
     payload}`` where ``response_item`` payloads carry ``{type: message, role,
     content: [{type: input_text|output_text, text}]}`` /
     ``function_call`` / ``function_call_output``, and ``event_msg`` payloads
     carry ``token_count`` / ``task_complete`` (with ``last_agent_message``).

   Consumers (brain_writer marker scan, tool_memory error scan, memory
   auto-save, Stop metrics) iterate records instead of hand-rolling the
   Claude JSONL shape — which this repo had done six separate times.
"""

import json
import os
from pathlib import Path
from typing import Iterator, List, Optional

from hooks.config import AGENTIHOOKS_HOME
from hooks.memory.records import CODEX_TYPES, TranscriptRecord, is_copilot_type, normalize_entries


def detect_transcript_format(path: str | Path) -> str:
    """Return ``"codex"``, ``"copilot"`` or ``"claude"`` by peeking at the first records.

    A single corrupt line (e.g. a rollout read mid-write) must not misdetect
    the whole file as claude — scan up to the first 5 lines that actually
    parse as JSON before giving up.
    """
    p = Path(path)
    try:
        with open(p, errors="replace") as f:
            checked = 0
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("["):
                    return "claude"  # bare-array variant is claude-only
                try:
                    first = json.loads(line)
                except json.JSONDecodeError:
                    continue
                checked += 1
                if first.get("type") in CODEX_TYPES:
                    return "codex"
                if is_copilot_type(first.get("type")):
                    return "copilot"
                if checked >= 5:
                    break
    except OSError:
        pass
    return "claude"


def _load_entries(path: Path) -> list[dict]:
    """Tolerant load: JSONL (skipping bad lines) or a bare JSON array."""
    try:
        content = path.read_text(errors="replace").strip()
    except OSError:
        return []
    if not content:
        return []
    if content.startswith("["):
        try:
            loaded = json.loads(content)
            return loaded if isinstance(loaded, list) else []
        except json.JSONDecodeError:
            return []
    entries = []
    for line in content.split("\n"):
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def iter_transcript_records(path: str | Path) -> Iterator[TranscriptRecord]:
    """Yield unified records from a Claude, Codex or Copilot transcript."""
    p = Path(path)
    if not p.exists():
        return
    entries = _load_entries(p)
    if not entries:
        return
    yield from normalize_entries(detect_transcript_format(p), entries)


_AGENT_LOG = os.getenv("AGENT_LOG_FILE", str(AGENTIHOOKS_HOME / "logs" / "agent.log"))


def _parse_entries(path: str) -> List[dict]:
    """Read agent.log JSONL and parse each line into a dict."""
    p = Path(path)
    if not p.exists():
        return []
    entries = []
    with open(p, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _extract_text(entry: dict) -> Optional[str]:
    """Extract readable text from a transcript entry.

    Reuses the same logic as hooks.observability.transcript.extract_content.
    """
    message = entry.get("message")

    if isinstance(message, str):
        return message

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


def search_transcripts(
    query: str,
    session_id: Optional[str] = None,
    limit: int = 20,
    log_path: Optional[str] = None,
) -> List[dict]:
    """Search agent.log for entries matching query.

    Returns list of {session_id, type, content, timestamp}.
    """
    path = log_path or _AGENT_LOG
    entries = _parse_entries(path)
    query_lower = query.lower()

    results = []
    for entry in reversed(entries):  # newest first
        if session_id and entry.get("session_id") != session_id:
            continue

        entry_type = entry.get("type", "unknown")
        if entry_type not in ("user", "assistant"):
            continue

        text = _extract_text(entry)
        if not text:
            continue

        if query_lower not in text.lower():
            continue

        results.append(
            {
                "session_id": entry.get("session_id", ""),
                "type": entry_type,
                "content": text[:2000],  # cap long entries
                "timestamp": entry.get("timestamp", ""),
            }
        )

        if len(results) >= limit:
            break

    return results


def get_session_transcript(
    session_id: str,
    last_n: int = 50,
    log_path: Optional[str] = None,
) -> List[dict]:
    """Get last N transcript entries for a specific session."""
    path = log_path or _AGENT_LOG
    entries = _parse_entries(path)

    session_entries = []
    for entry in entries:
        if entry.get("session_id") != session_id:
            continue

        entry_type = entry.get("type", "unknown")
        if entry_type not in ("user", "assistant"):
            continue

        text = _extract_text(entry)
        if not text:
            continue

        session_entries.append(
            {
                "session_id": session_id,
                "type": entry_type,
                "content": text[:2000],
                "timestamp": entry.get("timestamp", ""),
            }
        )

    # Return last N entries
    return session_entries[-last_n:]
