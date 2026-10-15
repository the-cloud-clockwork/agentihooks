"""One context budget per hook event, filled in priority order.

Claude Code moves any additionalContext or plain stdout above ten thousand
characters into a file and shows the model only its first two thousand, so
doctrine goes first and the lowest priority blocks give way.
"""

from __future__ import annotations

KINDS = ("inbox", "alert", "enforcement", "condition", "refocus", "other", "brain", "tool_memory")
# Rendering an inbox block marks its items delivered, so it is never shortened or dropped.
_WHOLE = frozenset({"inbox"})
_LABELS = {
    "inbox": "inbox messages",
    "alert": "alerts",
    "enforcement": "enforcements",
    "condition": "conditions",
    "refocus": "swarm refocus",
    "other": "notices",
    "brain": "brain feed and broadcasts",
    "tool_memory": "tool memory",
}
_NOTICE_ROOM = 200
_MIN_SHORTENED = 300


def units(text: str) -> int:
    """Length as Claude Code counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def budget_chars(target: str | None = None) -> int | None:
    from hooks.config import HOOK_INJECTION_MAX_CHARS
    from hooks.targets import current_target

    if (target or current_target()) != "claude" or HOOK_INJECTION_MAX_CHARS <= 0:
        return None
    return HOOK_INJECTION_MAX_CHARS


def _rank(kind: str) -> int:
    return KINDS.index(kind) if kind in KINDS else KINDS.index("other")


def _shorten(text: str, room: int) -> str:
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        cost = units(line) + (1 if kept else 0)
        if used + cost > room:
            break
        kept.append(line)
        used += cost
    if kept:
        return "\n".join(kept)
    cut = text[:room]
    while units(cut) > room:
        cut = cut[: len(cut) - max(1, (units(cut) - room + 1) // 2)]
    return cut


def fit(blocks: list[tuple[str, str]], budget: int | None, sep: str = "\n") -> str:
    """Join *blocks* (kind, text) highest priority first within *budget* characters; no budget keeps arrival order."""
    blocks = [b for b in blocks if b[1]]
    if budget is None:
        return sep.join(text for _, text in blocks)
    ordered = sorted(blocks, key=lambda b: _rank(b[0]))
    joined = sep.join(text for _, text in ordered)
    if units(joined) <= budget:
        return joined
    room = budget - _NOTICE_ROOM
    kept: list[str] = []
    dropped: list[str] = []
    shortened: list[str] = []
    for kind, text in ordered:
        avail = room - (units(sep) if kept else 0)
        whole = units(text) <= avail or kind in _WHOLE
        part = text if whole else _shorten(text, avail) if avail > 0 else ""
        if part is not text and units(part) < min(_MIN_SHORTENED, units(text) // 2):
            part = ""
        if part:
            kept.append(part)
            room = avail - units(part)
        if part is not text:
            (shortened if part else dropped).append(_LABELS.get(kind, _LABELS["other"]))
    notes = [f"left out {', '.join(dict.fromkeys(dropped))}"] if dropped else []
    if shortened:
        notes.append(f"shortened {', '.join(dict.fromkeys(shortened))}")
    kept.append(f"[context budget: {'; '.join(notes)} to stay under {budget} characters]")
    return sep.join(kept)
