"""Memory storage backend — Redis with JSONL file fallback.

Stores memories as hashes in Redis, indexed by sorted sets for efficient
retrieval by recency, tag, or session. Falls back to a local JSONL file
when Redis is unavailable.
"""

import json
import os
import uuid
from dataclasses import asdict, dataclass
from time import time
from typing import List, Optional


@dataclass
class Memory:
    id: str
    content: str
    summary: str
    tags: List[str]
    session_id: str
    source: str  # "manual" | "auto" | "transcript"
    agent: str
    created_at: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Memory":
        data = dict(data)  # shallow copy
        # Redis stores tags as JSON string
        if isinstance(data.get("tags"), str):
            data["tags"] = json.loads(data["tags"])
        if isinstance(data.get("created_at"), str):
            data["created_at"] = float(data["created_at"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})  # NOSONAR


from hooks._redis import redis_key
from hooks.config import AGENTIHOOKS_HOME

_KEY_PREFIX: str = os.getenv("REDIS_KEY_PREFIX", "agenticore")
_AGENT: str = os.getenv("AGENTICORE_AGENT", "unknown")


def _rkey(suffix: str) -> str:
    """Build namespaced Redis key for memory subsystem."""
    return f"{_KEY_PREFIX}:memory:{suffix}"


def _session_index(session_id: str) -> str:
    return redis_key("memory:idx:session", session_id)


class MemoryStore:
    """Persistent memory store with Redis + JSONL file fallback."""

    def __init__(self) -> None:
        self._redis = None
        self._redis_checked = False
        self._file_path = AGENTIHOOKS_HOME / "agent_memories.jsonl"

    # ------------------------------------------------------------------
    # Redis connection (lazy, fail-safe)
    # ------------------------------------------------------------------

    def _get_redis(self):
        if self._redis_checked:
            return self._redis
        self._redis_checked = True
        try:
            from hooks._redis import get_redis

            self._redis = get_redis()
        except Exception:  # NOSONAR — hooks must never crash the parent process
            self._redis = None
        return self._redis

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(
        self,
        content: str,
        tags: Optional[List[str]] = None,
        summary: Optional[str] = None,
        session_id: Optional[str] = None,
        source: str = "manual",
    ) -> Memory:
        """Persist a new memory. Returns the created Memory."""
        tags = tags or []
        memory = Memory(
            id=str(uuid.uuid4()),
            content=content,
            summary=summary or content[:100],
            tags=tags,
            session_id=session_id or "manual",
            source=source,
            agent=_AGENT,
            created_at=time(),
        )

        r = self._get_redis()
        if r is not None:
            self._redis_save(r, memory)
        else:
            self._file_save(memory)

        return memory

    def search(
        self,
        query: str,
        tags: Optional[List[str]] = None,
        limit: int = 10,
    ) -> List[Memory]:
        """Keyword search across content, summary, and tags."""
        r = self._get_redis()
        if r is not None:
            return self._redis_search(r, query, tags, limit)
        return self._file_search(query, tags, limit)

    def recall(
        self,
        session_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 10,
        since_hours: Optional[int] = None,
    ) -> List[Memory]:
        """Recall recent memories, optionally filtered."""
        r = self._get_redis()
        if r is not None:
            return self._redis_recall(r, session_id, tags, limit, since_hours)
        return self._file_recall(session_id, tags, limit, since_hours)

    def get(self, memory_id: str) -> Optional[Memory]:
        r = self._get_redis()
        if r is not None:
            return self._redis_get(r, memory_id)
        return self._file_get(memory_id)

    def delete(self, memory_id: str) -> bool:
        r = self._get_redis()
        if r is not None:
            return self._redis_delete(r, memory_id)
        return self._file_delete(memory_id)

    def clear(self) -> int:
        """Delete all memories. Returns count deleted."""
        r = self._get_redis()
        if r is not None:
            return self._redis_clear(r)
        return self._file_clear()

    def list_all(self, limit: int = 20, offset: int = 0) -> List[Memory]:
        r = self._get_redis()
        if r is not None:
            return self._redis_list(r, limit, offset)
        return self._file_list(limit, offset)

    # ------------------------------------------------------------------
    # Redis implementation
    # ------------------------------------------------------------------

    def _redis_save(self, r, mem: Memory) -> None:
        pipe = r.pipeline()
        hash_key = _rkey(mem.id)
        pipe.hset(
            hash_key,
            mapping={
                "id": mem.id,
                "content": mem.content,
                "summary": mem.summary,
                "tags": json.dumps(mem.tags),
                "session_id": mem.session_id,
                "source": mem.source,
                "agent": mem.agent,
                "created_at": str(mem.created_at),
            },
        )
        pipe.zadd(_rkey("idx:all"), {mem.id: mem.created_at})
        for tag in mem.tags:
            pipe.zadd(_rkey(f"idx:tag:{tag}"), {mem.id: mem.created_at})
        pipe.sadd(_session_index(mem.session_id), mem.id)
        pipe.execute()

    def _redis_get(self, r, memory_id: str) -> Optional[Memory]:
        data = r.hgetall(_rkey(memory_id))
        if not data:
            return None
        return Memory.from_dict(data)

    def _redis_search(self, r, query: str, tags: Optional[List[str]], limit: int) -> List[Memory]:
        query_lower = query.lower()
        # If tags provided, get intersection candidates; else use all index
        if tags:
            candidate_ids = set()
            for tag in tags:
                members = r.zrevrange(_rkey(f"idx:tag:{tag}"), 0, -1)
                if not candidate_ids:
                    candidate_ids = set(members)
                else:
                    candidate_ids &= set(members)
        else:
            candidate_ids = r.zrevrange(_rkey("idx:all"), 0, 499)

        results: List[Memory] = []
        for mid in candidate_ids:
            data = r.hgetall(_rkey(mid))
            if not data:
                continue
            searchable = f"{data.get('content', '')} {data.get('summary', '')} {data.get('tags', '')}".lower()
            if query_lower in searchable:
                results.append(Memory.from_dict(data))
                if len(results) >= limit:
                    break

        results.sort(key=lambda m: m.created_at, reverse=True)
        return results[:limit]

    def _redis_recall(self, r, session_id, tags, limit, since_hours) -> List[Memory]:
        min_score = "-inf"
        if since_hours:
            min_score = str(time() - since_hours * 3600)

        if session_id:
            all_ids = r.smembers(_session_index(session_id))
        elif tags:
            all_ids = set()
            for tag in tags:
                members = r.zrevrangebyscore(_rkey(f"idx:tag:{tag}"), "+inf", min_score)
                if not all_ids:
                    all_ids = set(members)
                else:
                    all_ids &= set(members)
        else:
            all_ids = r.zrevrangebyscore(_rkey("idx:all"), "+inf", min_score, start=0, num=limit)

        results: List[Memory] = []
        for mid in all_ids:
            data = r.hgetall(_rkey(mid))
            if not data:
                continue
            mem = Memory.from_dict(data)
            if since_hours and mem.created_at < time() - since_hours * 3600:
                continue
            results.append(mem)

        results.sort(key=lambda m: m.created_at, reverse=True)
        return results[:limit]

    def _redis_delete(self, r, memory_id: str) -> bool:
        data = r.hgetall(_rkey(memory_id))
        if not data:
            return False
        mem = Memory.from_dict(data)
        pipe = r.pipeline()
        pipe.delete(_rkey(memory_id))
        pipe.zrem(_rkey("idx:all"), memory_id)
        for tag in mem.tags:
            pipe.zrem(_rkey(f"idx:tag:{tag}"), memory_id)
        pipe.srem(_session_index(mem.session_id), memory_id)
        pipe.execute()
        return True

    def _redis_clear(self, r) -> int:
        all_ids = r.zrange(_rkey("idx:all"), 0, -1)
        if not all_ids:
            return 0

        pipe = r.pipeline()
        for mid in all_ids:
            data = r.hgetall(_rkey(mid))
            if data:
                mem = Memory.from_dict(data)
                for tag in mem.tags:
                    pipe.zrem(_rkey(f"idx:tag:{tag}"), mid)
                pipe.srem(_session_index(mem.session_id), mid)
            pipe.delete(_rkey(mid))
        pipe.delete(_rkey("idx:all"))
        pipe.execute()
        return len(all_ids)

    def _redis_list(self, r, limit: int, offset: int) -> List[Memory]:
        ids = r.zrevrange(_rkey("idx:all"), offset, offset + limit - 1)
        results = []
        for mid in ids:
            data = r.hgetall(_rkey(mid))
            if data:
                results.append(Memory.from_dict(data))
        return results

    # ------------------------------------------------------------------
    # File fallback implementation
    # ------------------------------------------------------------------

    def _file_read_all(self) -> List[dict]:
        if not self._file_path.exists():
            return []
        entries = []
        with open(self._file_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries

    def _file_write_all(self, entries: List[dict]) -> None:
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._file_path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def _file_save(self, mem: Memory) -> None:
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._file_path, "a") as f:
            f.write(json.dumps(mem.to_dict()) + "\n")

    def _file_search(self, query: str, tags: Optional[List[str]], limit: int) -> List[Memory]:
        query_lower = query.lower()
        entries = self._file_read_all()
        results = []
        for entry in reversed(entries):  # newest first
            searchable = (
                f"{entry.get('content', '')} {entry.get('summary', '')} {json.dumps(entry.get('tags', []))}".lower()
            )
            if query_lower not in searchable:
                continue
            if tags and not set(tags).issubset(set(entry.get("tags", []))):
                continue
            results.append(Memory.from_dict(entry))
            if len(results) >= limit:
                break
        return results

    def _file_recall(self, session_id, tags, limit, since_hours) -> List[Memory]:
        entries = self._file_read_all()
        cutoff = time() - since_hours * 3600 if since_hours else 0
        results = []
        for entry in reversed(entries):
            created = float(entry.get("created_at", 0))
            if since_hours and created < cutoff:
                continue
            if session_id and entry.get("session_id") != session_id:
                continue
            if tags and not set(tags).issubset(set(entry.get("tags", []))):
                continue
            results.append(Memory.from_dict(entry))
            if len(results) >= limit:
                break
        return results

    def _file_get(self, memory_id: str) -> Optional[Memory]:
        for entry in self._file_read_all():
            if entry.get("id") == memory_id:
                return Memory.from_dict(entry)
        return None

    def _file_delete(self, memory_id: str) -> bool:
        entries = self._file_read_all()
        new_entries = [e for e in entries if e.get("id") != memory_id]
        if len(new_entries) == len(entries):
            return False
        self._file_write_all(new_entries)
        return True

    def _file_clear(self) -> int:
        entries = self._file_read_all()
        count = len(entries)
        if self._file_path.exists():
            self._file_path.write_text("")
        return count

    def _file_list(self, limit: int, offset: int) -> List[Memory]:
        entries = self._file_read_all()
        entries.sort(key=lambda e: float(e.get("created_at", 0)), reverse=True)
        sliced = entries[offset : offset + limit]
        return [Memory.from_dict(e) for e in sliced]
