"""Doctor detectors over swarm handoffs: each with its document, the seat's recaps and the successor's questions."""

import re

from scripts.doctor.words import gist, plural
from scripts.swarm.health.findings import Finding

REASK_SHARE = 0.6
REASK_WORDS = 4
WORD_RE = re.compile(r"[a-z][a-z0-9_-]{3,}")
COMMON = frozenset(
    "about also been before does from have into just should than that them then there they this were what when "
    "where which will with would your".split()
)


def findings(handoffs):
    return [*missing_recap(handoffs), *reasked(handoffs)]


def _words(text):
    return set(WORD_RE.findall(text.lower())) - COMMON


def missing_recap(handoffs):
    found = []
    for h in handoffs:
        if any(r["occupant"] == h["from"] for r in h["recaps"]):
            continue
        found.append(
            Finding(
                "missing recap",
                h["from"] or h["seat"],
                "handed off without a recap",
                (
                    f"task {h['task']} on seat {h['seat']}",
                    f"handed off by {h['from']}"
                    if h["from"]
                    else "handed off by an occupant the seat history predates",
                    f"successor {h['to']}" if h["to"] else "successor not started yet",
                    plural(len(h["recaps"]), "recap") + " on the seat from earlier occupants",
                ),
                "every handoff leaves a recap on its seat",
                1,
            )
        )
    return found


def reasked(handoffs, share=REASK_SHARE):
    found = []
    for h in handoffs:
        known = _words(h["document"]).union(*(_words(r["text"]) for r in h["recaps"] if r["occupant"] == h["from"]))
        evidence = []
        for message in h["asked"]:
            asked = _words(message["text"])
            if "?" not in message["text"] or len(asked) < REASK_WORDS:
                continue
            overlap = len(asked & known) / len(asked)
            if overlap >= share:
                evidence.append(
                    f"message {message['id']} to {message['address']}: {gist(message['text'])} "
                    f"({round(overlap * 100)}% of its words are in the handoff)"
                )
        if evidence:
            found.append(
                Finding(
                    "reasked handoff",
                    h["to"],
                    f"asked what the handoff from {h['from']} already answered",
                    (f"task {h['task']} on seat {h['seat']}", *evidence),
                    f"a question sharing at least {round(share * 100)}% of its words with the handoff",
                    len(evidence),
                )
            )
    return found
