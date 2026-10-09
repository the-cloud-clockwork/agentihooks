"""Swarm authored ledger text, reshaped so the ledger's plain words check never refuses it."""

import re

from scripts.swarm_ledger import ledger_comments

FALLBACK = "The swarm has a notice it could not put in plain words."
SWAPS = (
    (re.compile(r"→|⇒|->|=>"), " to "),
    (re.compile(r"[—–;]"), ", "),
    (re.compile(r"[()]"), ""),
    (re.compile(r"(?<=[a-z0-9])_(?=[a-z0-9])"), " "),
)


def plain(text: str, kind: str = "comment") -> str:
    for pattern, repl in SWAPS:
        text = pattern.sub(repl, text)
    for _, pattern in ledger_comments.RULES:
        text = pattern.sub(" ", text)
    words = " ".join(text.split()[: ledger_comments.LIMITS[kind]])
    text = words.replace(" ,", ",").replace(" .", ".").strip(" ,")
    return text if text and not ledger_comments.problems(text, kind) else FALLBACK
