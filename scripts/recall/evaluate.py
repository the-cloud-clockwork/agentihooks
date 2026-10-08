import json
from pathlib import Path

from .search import Filters, search
from .store import SQLiteRecallStore

TOP = 5


class GoldenError(ValueError):
    pass


def load_golden(path: Path) -> list[dict]:
    try:
        entries = json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        raise GoldenError(f"cannot read golden file {path}") from None
    if not isinstance(entries, list) or not entries:
        raise GoldenError(f"golden file holds no questions: {path}")
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or not entry.get("question") or not entry.get("expect"):
            raise GoldenError(f"golden entry {number} needs a question and an expect")
    return entries


def evaluate(store: SQLiteRecallStore, entries: list[dict], now: int | None = None) -> dict:
    misses = []
    for entry in entries:
        expect = [entry["expect"]] if isinstance(entry["expect"], str) else list(entry["expect"])
        filters = Filters(scope=entry.get("scope", "all"), kinds=tuple(entry.get("kinds", ())))
        hits = search(store, entry["question"], filters, TOP, now=now)
        if not set(expect) & {name for hit in hits for name in (hit.ref, hit.item)}:
            misses.append({"question": entry["question"], "expect": expect, "found": [hit.ref for hit in hits]})
    found = len(entries) - len(misses)
    return {"questions": len(entries), "hits": found, "top5_hit_rate": round(found / len(entries), 4), "misses": misses}
