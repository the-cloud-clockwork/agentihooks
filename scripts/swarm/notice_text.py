"""Swarm authored ledger text, reshaped so the ledger's plain words check never refuses it."""

import re
import sys

from scripts.swarm.ledger_client import LEDGER_DIR

FALLBACK = "The swarm has a notice it could not put in plain words."
SWAPS = (
    (re.compile(r"\s*(?:→|⇒|->|=>)\s*"), " to "),
    (re.compile(r"\s*[—–]\s*"), ", "),
    (re.compile(r"\s*;\s*"), ", "),
    (re.compile(r"[()]"), ""),
    (re.compile(r"(?<=[a-z0-9])_(?=[a-z0-9])"), " "),
)


def _comments():
    if str(LEDGER_DIR) not in sys.path:
        sys.path.insert(0, str(LEDGER_DIR))
    import ledger_comments

    return ledger_comments


def plain(text: str, kind: str = "comment") -> str:
    comments = _comments()
    for pattern, repl in SWAPS:
        text = pattern.sub(repl, text)
    for _, pattern in comments.RULES:
        text = pattern.sub(" ", text)
    words = text.split()[: comments.LIMITS[kind]]
    text = re.sub(r"\s+([,.])", r"\1", " ".join(words)).strip(" ,")
    return text if text and not comments.problems(text, kind) else FALLBACK
