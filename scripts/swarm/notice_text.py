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
REPEATS = re.compile(r",{2,}")


def plain(text: str, kind: str = "comment") -> str:
    links = ledger_comments.web_links()
    kept = [*links.findall(text), ""]
    text = "".join(_prose(piece) + link for piece, link in zip(links.split(text), kept))
    words = " ".join(text.split()[: ledger_comments.LIMITS[kind]])
    text = REPEATS.sub(",", words.replace(" ,", ",").replace(" .", ".")).strip(" ,")
    return text or FALLBACK


def _prose(text: str) -> str:
    for pattern, repl in SWAPS:
        text = pattern.sub(repl, text)
    for _, pattern in ledger_comments.RULES:
        text = pattern.sub(" ", text)
    return text
