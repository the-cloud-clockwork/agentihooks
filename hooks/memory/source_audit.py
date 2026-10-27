"""Source fidelity audit of legacy transcript service history before it is imported into the session corpus."""

import copy
import hashlib
import json
from dataclasses import dataclass

from hooks.memory.api.ingest import Rejected, digest

FULL_RAW, PREVIEW_ONLY, VECTOR_ONLY, MISSING = "full_raw", "preview_only", "vector_only", "missing"
CLASSES = (MISSING, VECTOR_ONLY, PREVIEW_ONLY, FULL_RAW)
RANK = {name: rank for rank, name in enumerate(CLASSES)}
CONFIDENCE = {FULL_RAW: "lossless", PREVIEW_ONLY: "lossy", VECTOR_ONLY: "fragment", MISSING: "none"}
METRIC = "source_fidelity_classes"
ENTRY_TYPES = ("user", "assistant", "summary")
OBSERVATION_FIELDS = frozenset({"source", "raw", "raw_sha256", "preview", "vectors"})
PREVIEW_FIELDS = frozenset({"entry_type", "timestamp", "content", "tool_names", "uuid"})
VECTOR_FIELDS = frozenset({"chunk_idx", "timestamp", "text_preview"})
BAD_SOURCE = "a source observation needs a name and only raw bytes, a preview list and vector rows of the legacy shape"
HASH_MISMATCH = "the raw bytes do not match the hash recorded for this source"
STALE = "the audit moved past this revision; read it again and rerun"
DUPLICATE = "each source may appear once in a run; merge its raw, preview and vector rows into one observation"
NOT_LOSSLESS = "only a source with its full raw transcript may be labelled a lossless import"
UNKNOWN = "this source has not been audited"


@dataclass(frozen=True)
class LegacyLimits:
    preview_chars: int = 500
    preview_entries: int = 500
    vector_chars: int = 1000


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content(entry: dict) -> tuple[str, list[str]]:
    if entry["type"] == "summary":
        return str(entry.get("summary", "")), []
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content, []
    blocks = [block for block in content or [] if isinstance(block, dict)]
    text = "\n".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    return text, [block.get("name", "") for block in blocks if block.get("type") == "tool_use"]


def raw_entries(data: bytes) -> tuple[list[dict], int]:
    entries, unparsed = [], 0
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            unparsed += 1
            continue
        if isinstance(entry, dict) and entry.get("type") in ENTRY_TYPES:
            text, tools = _content(entry)
            entries.append({"uuid": entry.get("uuid"), "entry_type": entry["type"], "content": text, "tools": tools})
    return entries, unparsed


def turns(entries: list[dict]) -> list[str]:
    chunks, current = [], ""
    for entry in entries:
        kind, text = entry["entry_type"], entry["content"]
        if kind == "user":
            if current:
                chunks.append(current)
            current = f"User: {text}\n"
        elif kind == "assistant":
            current += f"Assistant: {text}\n"
            if entry["tools"]:
                current += f"Tools used: {', '.join(entry['tools'])}\n"
        else:
            current += f"Summary: {text}\n"
    return [*chunks, current] if current else chunks


def _ranges(indices: list[int]) -> list[list[int]]:
    spans = []
    for index in indices:
        if spans and spans[-1][1] == index - 1:
            spans[-1][1] = index
        else:
            spans.append([index, index])
    return spans


def _gaps(entries: list[dict], preview: list[dict], limits: LegacyLimits) -> list[dict]:
    held = {entry["uuid"] for entry in preview}
    present = [index for index, entry in enumerate(entries) if entry["uuid"] in held]
    absent = [index for index, entry in enumerate(entries) if entry["uuid"] not in held]
    if not present:
        return [{"start": start, "end": end, "cause": "unplaced"} for start, end in _ranges(absent)]
    first, last = present[0], present[-1]
    head = "trimmed_head" if len(preview) >= limits.preview_entries else "missing_head"
    found = []
    for start, end in _ranges(absent):
        cause = head if end + 1 == first else "uncollected_tail" if start - 1 == last else "incremental_offset_gap"
        found.append({"start": start, "end": end, "cause": cause})
    return found


def _preview_loss(entries: list[dict], preview: list[dict], limits: LegacyLimits) -> dict:
    by_uuid = {entry["uuid"]: entry["content"] for entry in entries}
    capped = diverged = 0
    for item in preview:
        full = by_uuid.get(item["uuid"])
        if full is None:
            continue
        capped += len(full) > limits.preview_chars and item["content"] == full[: limits.preview_chars]
        diverged += item["content"] != full[: limits.preview_chars]
    return {
        "preview_entries": len(preview),
        "capped": capped,
        "diverged": diverged,
        "missing_ranges": _gaps(entries, preview, limits),
    }


def _vector_loss(chunks: list[str], vectors: list[dict], limits: LegacyLimits) -> dict:
    where = {}
    for index, chunk in enumerate(chunks):
        where.setdefault(chunk[: limits.vector_chars], index)
    renumbered, orphans, matched = [], 0, set()
    for row in sorted(vectors, key=lambda row: row["chunk_idx"]):
        turn = where.get(row["text_preview"])
        if turn is None:
            orphans += 1
            continue
        matched.add(turn)
        if turn != row["chunk_idx"]:
            renumbered.append({"chunk_idx": row["chunk_idx"], "turn": turn})
    return {
        "vector_rows": len(vectors),
        "renumbered": renumbered,
        "orphans": orphans,
        "turns_without_vector": len(chunks) - len(matched),
    }


def _shape(observation: object) -> bool:
    if not isinstance(observation, dict) or not set(observation) <= OBSERVATION_FIELDS:
        return False
    if not isinstance(observation.get("source"), str) or not observation["source"]:
        return False
    raw, preview, vectors = (observation.get(key) for key in ("raw", "preview", "vectors"))
    if raw is not None and not isinstance(raw, bytes):
        return False
    if preview is not None and not _rows(preview, PREVIEW_FIELDS, {"uuid": str, "content": str}):
        return False
    return vectors is None or _rows(vectors, VECTOR_FIELDS, {"chunk_idx": int, "text_preview": str})


def _rows(rows: object, fields: frozenset, types: dict) -> bool:
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            return False
        if not all(isinstance(row[key], kind) and not isinstance(row[key], bool) for key, kind in types.items()):
            return False
    return True


def audit_source(observation: object, limits: LegacyLimits = LegacyLimits()) -> dict:
    if not _shape(observation):
        raise Rejected("bad_source", BAD_SOURCE)
    raw, preview, vectors = (observation.get(key) for key in ("raw", "preview", "vectors"))
    if raw is not None and observation.get("raw_sha256", _sha(raw)) != _sha(raw):
        raise Rejected("raw_hash_mismatch", HASH_MISMATCH)
    found = FULL_RAW if raw is not None else PREVIEW_ONLY if preview else VECTOR_ONLY if vectors else MISSING
    result = {
        "source": observation["source"],
        "class": found,
        "confidence": CONFIDENCE[found],
        "raw_sha256": _sha(raw) if raw is not None else None,
        "preview_sha256": digest(preview) if preview else None,
        "vectors_sha256": digest(vectors) if vectors else None,
    }
    if raw is None:
        result["preview"] = _unanchored_preview(preview or [], limits)
        result["vectors"] = {"vector_rows": len(vectors or []), "preview_chars": limits.vector_chars}
        return result
    entries, unparsed = raw_entries(raw)
    result["raw"] = {"entries": len(entries), "turns": len(turns(entries)), "unparsed_lines": unparsed}
    result["preview"] = _preview_loss(entries, preview, limits) if preview else None
    result["vectors"] = _vector_loss(turns(entries), vectors, limits) if vectors else None
    return result


def _unanchored_preview(preview: list[dict], limits: LegacyLimits) -> dict:
    return {
        "preview_entries": len(preview),
        "possibly_capped": sum(len(item["content"]) >= limits.preview_chars for item in preview),
        "possibly_trimmed": len(preview) >= limits.preview_entries,
        "missing_ranges": "unknown",
    }


class SourceAudit:
    def __init__(self, state: dict | None = None, limits: LegacyLimits = LegacyLimits()) -> None:
        self.state = copy.deepcopy(state) if state else {"revision": 0, "sources": {}}
        self.limits = limits

    def _outcome(self, found: dict) -> str:
        held = self.state["sources"].get(found["source"])
        if held is None:
            return "added"
        if held == found:
            return "replayed"
        if RANK[found["class"]] > RANK[held["class"]]:
            return "upgraded"
        return "updated" if found["class"] == held["class"] else "kept"

    def run(self, observations: list, revision: int) -> dict:
        found = [audit_source(observation, self.limits) for observation in observations]
        if len({item["source"] for item in found}) != len(found):
            raise Rejected("duplicate_source", DUPLICATE)
        outcomes = [(item, self._outcome(item)) for item in found]
        changes = [(item, outcome) for item, outcome in outcomes if outcome in ("added", "upgraded", "updated")]
        if changes and revision <= self.state["revision"]:
            raise Rejected("stale_revision", STALE)
        for item, _ in changes:
            self.state["sources"][item["source"]] = item
        if changes:
            self.state["revision"] = revision
        return {item["source"]: {"outcome": outcome, "observed": item["class"]} for item, outcome in outcomes}

    def certify(self, source: str) -> dict:
        held = self.state["sources"].get(source)
        if held is None:
            raise Rejected("unknown_source", UNKNOWN)
        if held["class"] != FULL_RAW:
            raise Rejected("not_lossless", NOT_LOSSLESS)
        return {"source": source, "label": "lossless", "raw_sha256": held["raw_sha256"]}

    def report(self) -> dict:
        held = [self.state["sources"][name] for name in sorted(self.state["sources"])]
        listed = {name: [item["source"] for item in held if item["class"] == name] for name in reversed(CLASSES)}
        return {
            "revision": self.state["revision"],
            "sources": held,
            **listed,
            METRIC: {name: len(listed[name]) for name in reversed(CLASSES)},
        }

    def dumps(self) -> str:
        return json.dumps(self.state, sort_keys=True)

    @classmethod
    def loads(cls, text: str, limits: LegacyLimits = LegacyLimits()) -> "SourceAudit":
        return cls(json.loads(text), limits)
