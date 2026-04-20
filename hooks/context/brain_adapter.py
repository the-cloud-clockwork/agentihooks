"""Brain adapter — pluggable source-to-channel bridge for brain content injection.

Reads brain content from a configurable source (file/API), publishes to a
broadcast channel. The broadcast system handles per-turn delivery to
subscribed sessions.

Source types:
    file — reads markdown files from a directory (NFS mount, vault path, etc.)
    mcp  — (future) fetches from an MCP tool or API endpoint

Brain files use YAML frontmatter:
    ---
    id: hot-arcs-2026-04-10
    title: Active Hot Arcs
    priority: 10
    ttl: 3600
    severity: info
    ---
    Content here...

Config (env vars via hooks.config):
    BRAIN_ENABLED (bool, default False)
    BRAIN_SOURCE_TYPE (str, default "file")
    BRAIN_SOURCE_PATH (str, default ~/.agentihooks/brain)
    BRAIN_CHANNEL (str, default "brain")
    BRAIN_REFRESH_INTERVAL (int, default 30 turns)

Public API:
    maybe_refresh(session_id)  — called from on_user_prompt_submit, counter-gated
    force_refresh()            — force re-read and republish
    get_status()               — return current brain state dict
    inject_on_session_start()  — one-shot injection at session start
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from hooks._redis import get_redis, redis_key
from hooks.common import log

# In-memory state
_memory_counter: dict[str, int] = {}
_content_hash: str = ""
_HASH_CACHE_FILE = Path.home() / ".agentihooks" / "brain_adapter_hash.json"


def _load_persisted_hash() -> str:
    """Read last-published content hash from disk. Empty on miss."""
    try:
        if _HASH_CACHE_FILE.exists():
            return json.loads(_HASH_CACHE_FILE.read_text()).get("hash", "")
    except Exception:
        pass
    return ""


def _save_persisted_hash(hash_val: str) -> None:
    """Persist last-published content hash so next hook process can dedup."""
    try:
        _HASH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HASH_CACHE_FILE.write_text(json.dumps({"hash": hash_val}))
    except Exception:
        pass


@dataclass
class BrainEntry:
    id: str
    title: str
    content: str
    priority: int = 5
    ttl: int = 3600
    severity: str = "info"
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Source interface + implementations
# ---------------------------------------------------------------------------


class BrainSource:
    """Abstract interface for brain content sources."""

    def fetch(self) -> list[BrainEntry]:
        raise NotImplementedError


class FileBrainSource(BrainSource):
    """Reads brain content from a directory of markdown files with YAML frontmatter."""

    def __init__(self, brain_dir: str | Path):
        self.brain_dir = Path(brain_dir).expanduser()

    def fetch(self) -> list[BrainEntry]:
        if not self.brain_dir.is_dir():
            return []

        entries = []
        for md_file in sorted(self.brain_dir.glob("*.md")):
            try:
                text = md_file.read_text()
                fm, body = _parse_frontmatter(text)
                if not body.strip():
                    continue
                entries.append(BrainEntry(
                    id=fm.get("id", md_file.stem),
                    title=fm.get("title", md_file.stem),
                    content=body.strip(),
                    priority=int(fm.get("priority", 5)),
                    ttl=int(fm.get("ttl", 3600)),
                    severity=fm.get("severity", "info"),
                    metadata=fm,
                ))
            except Exception as e:
                log("brain_adapter: failed to read file", {"file": str(md_file), "error": str(e)})

        # Sort by priority descending (higher priority first)
        entries.sort(key=lambda e: e.priority, reverse=True)
        return entries


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown text. Returns (frontmatter_dict, body)."""
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    fm_text = parts[1].strip()
    body = parts[2]

    fm = {}
    for line in fm_text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip().strip('"').strip("'")

    return fm, body


# ---------------------------------------------------------------------------
# Source factory
# ---------------------------------------------------------------------------


def _get_source() -> BrainSource | None:
    """Create the configured brain source."""
    try:
        from hooks.config import BRAIN_SOURCE_PATH, BRAIN_SOURCE_TYPE
    except ImportError:
        return None

    if BRAIN_SOURCE_TYPE == "file":
        return FileBrainSource(BRAIN_SOURCE_PATH)
    # Future: elif BRAIN_SOURCE_TYPE == "mcp": return McpBrainSource(...)
    return None


# ---------------------------------------------------------------------------
# Publish to channel
# ---------------------------------------------------------------------------


def _trim_hot_arcs_table(content: str, top_n: int) -> str:
    """If content contains a markdown table with header 'Arc | Heat | ...', keep top_n rows."""
    lines = content.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("| Arc") and "Heat" in stripped:
            header_idx = i
            break
    if header_idx is None or header_idx + 2 >= len(lines):
        return content
    sep_idx = header_idx + 1
    data_start = header_idx + 2
    data_end = data_start
    while data_end < len(lines) and lines[data_end].strip().startswith("|"):
        data_end += 1
    kept = lines[data_start:data_start + top_n]
    dropped = (data_end - data_start) - len(kept)
    tail = lines[data_end:]
    result = lines[:sep_idx + 1] + kept
    if dropped > 0:
        result.append(f"| ... | ... | ... | ... | *({dropped} more arcs trimmed)* |")
    result.extend(tail)
    return "\n".join(result)


def _shrink_entry(entry: BrainEntry, top_n: int, max_bytes: int) -> BrainEntry:
    content = entry.content
    if top_n > 0:
        content = _trim_hot_arcs_table(content, top_n)
    if max_bytes > 0 and len(content) > max_bytes:
        content = content[:max_bytes] + f"\n…[+{len(entry.content) - max_bytes} bytes trimmed]"
    if content == entry.content:
        return entry
    return BrainEntry(
        id=entry.id,
        title=entry.title,
        content=content,
        priority=entry.priority,
        ttl=entry.ttl,
        severity=entry.severity,
        metadata=entry.metadata,
    )


def _publish_entries(entries: list[BrainEntry]) -> int:
    """Publish brain entries to the broadcast channel. Returns count published."""
    try:
        from hooks.config import (
            BRAIN_CHANNEL,
            BRAIN_HOT_ARCS_TOP_N,
            BRAIN_PAYLOAD_MAX_BYTES,
        )
    except ImportError:
        return 0

    from hooks.context.broadcast import clear_broadcasts, create_broadcast
    from hooks.telemetry import span_ctx

    entries = [_shrink_entry(e, BRAIN_HOT_ARCS_TOP_N, BRAIN_PAYLOAD_MAX_BYTES) for e in entries]
    total_bytes = sum(len(e.content) for e in entries)
    with span_ctx(
        "brain.inject",
        {
            "channel": BRAIN_CHANNEL,
            "entry_count": len(entries),
            "total_bytes": total_bytes,
        },
    ) as span:
        # Clear existing brain messages on this channel
        clear_broadcasts(channel=BRAIN_CHANNEL)

        count = 0
        for entry in entries:
            msg_id = create_broadcast(
                message=f"[{entry.title}]\n{entry.content}",
                severity=entry.severity,
                ttl_seconds=entry.ttl,
                source="brain-adapter",
                persistent=True,  # Brain content should be persistent (every turn)
                channel=BRAIN_CHANNEL,
            )
            if msg_id:
                count += 1

        span.set_attrs({"published_count": count})
        return count


def _compute_hash(entries: list[BrainEntry]) -> str:
    """Compute a hash of entries to detect changes."""
    raw = json.dumps(
        [{"id": e.id, "content": e.content, "priority": e.priority} for e in entries],
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Turn counter (same pattern as context_refresh)
# ---------------------------------------------------------------------------


def _get_counter(session_id: str) -> int:
    r = get_redis()
    if r is not None:
        try:
            val = r.get(redis_key("brain_adapter", session_id))
            return int(val) if val else 0
        except Exception:
            pass
    return _memory_counter.get(session_id, 0)


def _set_counter(session_id: str, value: int) -> None:
    r = get_redis()
    if r is not None:
        try:
            r.set(redis_key("brain_adapter", session_id), value, ex=86400)
            return
        except Exception:
            pass
    _memory_counter[session_id] = value


def clear_session_state(session_id: str) -> None:
    r = get_redis()
    if r is not None:
        try:
            r.delete(redis_key("brain_adapter", session_id))
        except Exception:
            pass
    _memory_counter.pop(session_id, None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def maybe_refresh(session_id: str) -> bool:
    """Called on UserPromptSubmit. Counter-gated refresh. Returns True if refreshed."""
    global _content_hash

    try:
        from hooks.config import BRAIN_ENABLED, BRAIN_REFRESH_INTERVAL
    except ImportError:
        return False

    if not BRAIN_ENABLED:
        return False

    count = _get_counter(session_id) + 1
    _set_counter(session_id, count)

    interval = max(1, BRAIN_REFRESH_INTERVAL)
    if count % interval != 0 and count != 1:
        return False

    return force_refresh()


def force_refresh() -> bool:
    """Force re-read source and republish if changed. Returns True if published."""
    global _content_hash

    # Hook runs in fresh Python process every prompt — module-level
    # _content_hash starts empty. Hydrate it from persisted file so we can
    # dedup across process boundaries. Without this, every prompt re-publishes
    # identical content with a fresh expires_at timestamp, which defeats
    # prompt caching (every broadcast block differs by one UUID + one Z-time).
    if not _content_hash:
        _content_hash = _load_persisted_hash()

    source = _get_source()
    if source is None:
        return False

    try:
        entries = source.fetch()
    except Exception as e:
        log("brain_adapter: source fetch failed", {"error": str(e)})
        return False

    if not entries:
        # No brain content — clear channel
        try:
            from hooks.config import BRAIN_CHANNEL
            from hooks.context.broadcast import clear_broadcasts

            clear_broadcasts(channel=BRAIN_CHANNEL)
        except Exception:
            pass
        _content_hash = ""
        return False

    new_hash = _compute_hash(entries)
    if new_hash == _content_hash:
        return False

    count = _publish_entries(entries)
    _content_hash = new_hash
    _save_persisted_hash(new_hash)
    log("brain_adapter: published", {"count": count, "hash": new_hash})
    return True


def inject_on_session_start() -> bool:
    """One-shot injection at session start. Publishes brain content immediately."""
    try:
        from hooks.config import BRAIN_ENABLED
    except ImportError:
        return False

    if not BRAIN_ENABLED:
        return False

    return force_refresh()


def get_status() -> dict:
    """Return current brain adapter state."""
    try:
        from hooks.config import (
            BRAIN_CHANNEL,
            BRAIN_ENABLED,
            BRAIN_REFRESH_INTERVAL,
            BRAIN_SOURCE_PATH,
            BRAIN_SOURCE_TYPE,
        )
    except ImportError:
        return {"enabled": False, "error": "config not loaded"}

    source = _get_source()
    entry_count = 0
    if source:
        try:
            entry_count = len(source.fetch())
        except Exception:
            pass

    return {
        "enabled": BRAIN_ENABLED,
        "source_type": BRAIN_SOURCE_TYPE,
        "source_path": str(BRAIN_SOURCE_PATH),
        "channel": BRAIN_CHANNEL,
        "refresh_interval": BRAIN_REFRESH_INTERVAL,
        "entry_count": entry_count,
        "content_hash": _content_hash,
    }
