"""Relay: the master posts a decision the operator gave it in its own pane, as the operator's entry.

A question takes it as an answer, any other item as a comment. Only the ledger's orchestrator may relay,
and only words the hooks recorded from the operator in a master or planner session of its swarm, at any time;
the entry carries those words, of any length and in any wording.
"""

import re

ITEM_RE = re.compile(r"^(phases|questions|followups|tasks|notes)/[^/]+$")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
OPS = ("relay",)
RELAYED_FROM = "master pane"
MAX_TEXT = 20000


def check(op):
    by = op.get("by")
    if not isinstance(by, str) or not AUTHOR_RE.match(by) or by == "operator":
        raise ValueError("relay needs by, the relaying agent's name")
    if not ITEM_RE.match(str(op.get("item"))):
        raise ValueError("relay needs item <list>/<id>")
    text = op.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f"relay needs text up to {MAX_TEXT} characters")
    if not isinstance(op.get("quote"), str) or not op["quote"].strip():
        raise ValueError("relay needs quote, the operator's words")


def speakers(by):
    """Every address of the relaying agent, then every master and planner of its swarm with recorded words."""
    from hooks.context import operator_words
    from scripts.swarm.naming import addresses, parse

    names = addresses(by)
    codes = sorted({agent.code for agent in map(parse, names) if agent})
    crew = [n for code in codes for kind in ("master", "planner") for n in operator_words.recorded(f"{kind}@{code}-*")]
    return list(dict.fromkeys([*names, *crew]))


def verified(by, quote):
    """The quoted span, as the operator wrote it, of recorded words from any master or planner of the relaying agent's swarm."""
    from hooks.context import operator_words

    words = next((w for name in speakers(by) if (w := operator_words.matching(name, quote, within=None))), "")
    found = re.search(r"\s+".join(map(re.escape, quote.split())), words, re.IGNORECASE)
    return found.group(0) if found else ""


def apply(doc, op, ctx):
    name, item_id = op["item"].split("/")
    item = next((i for i in doc[name] if i["id"] == item_id), None)
    if item is None:
        return False
    thread = "answers" if name == "questions" else "comments"
    if any(e["id"] == op["id"] for e in item[thread]):
        return True
    master = ctx.meta["members"].get(op["by"], {}).get("role") == "orchestrator"
    words = verified(op["by"], op["quote"]) if master else ""
    if not words:
        return False
    marks = {"relayed_by": op["by"], "relayed_from": RELAYED_FROM, "quote": words}
    text = op["text"].strip()
    item[thread].append({"id": op["id"], "by": "operator", "at": ctx.at, "text": text, **marks})
    noun = "answer" if thread == "answers" else "comment"
    ctx.record("operator", f"{noun} added", op["item"], id=op["id"], text=text, **marks)
    ctx.stamp(f"{op['item']}/{thread}", "operator")
    return True
