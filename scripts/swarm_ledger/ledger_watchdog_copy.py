"""The newest watchdog document copy, stored on the ledger so every tick host reads the same copy."""

import hashlib
import re
from typing import Any

OPS = ("watchdog_copy",)
FIELD = "watchdog_copy"
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
MAX_COPY = 100000


def check(op: dict[str, Any]) -> None:
    by, text = op.get("by"), op.get("text")
    if set(op) != {"op", "id", "by", "text"} or not isinstance(by, str) or not AUTHOR_RE.match(by):
        raise ValueError("watchdog_copy takes id, by (an author name) and text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_COPY:
        raise ValueError(f"the watchdog copy must be text of at most {MAX_COPY} characters")


def apply(doc: dict[str, Any], op: dict[str, str], ctx: Any) -> bool:
    digest = hashlib.sha256(op["text"].encode()).hexdigest()
    held: dict[str, Any] = doc.get(FIELD) or {}
    if held.get("digest") == digest:
        return False
    doc[FIELD] = {"text": op["text"], "digest": digest, "at": ctx.at, "by": op["by"]}
    ctx.stamp(FIELD, op["by"])
    ctx.record(op["by"], "watchdog copy stored", FIELD, id=op["id"])
    return True
