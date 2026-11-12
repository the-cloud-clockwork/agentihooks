"""Relay: the master posts a decision the operator gave it in its own pane, as the operator's entry.

A question takes it as an answer, any other item as a comment, and the notes list as a new note.
Only the ledger's orchestrator may relay, and only words the hooks recorded from the operator in a master
or planner session of its swarm, at any time;
the entry carries those words, of any length and in any wording.
"""

import re
from typing import Any

from scripts.swarm_ledger import ports

ITEM_RE = re.compile(r"^((phases|questions|followups|tasks|notes)/[^/]+|notes)$")
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


def watched_masters(by: str) -> list[str]:
    from redis.exceptions import RedisError

    from scripts.doctor.priming import TEMPLATE

    get_redis = ports.hooks().redis
    operator_words = ports.hooks().operator_words
    RedisStore, SwarmError = ports.swarm().store.RedisStore, ports.swarm().store.SwarmError

    redis = get_redis()
    if redis is None:
        return []
    store = RedisStore(redis)
    try:
        slug = store.names.slug_of(by)
        watched = store.peer(slug)
        if not watched or store.config(slug).template != TEMPLATE:
            return []
        code = store.names.code_of(watched)
    except (RedisError, SwarmError):
        return []
    return operator_words.recorded(f"master@{code}-*") if code else []


def speakers(by):
    """Every address of the relaying agent, then every master and planner of its swarm with recorded words."""
    operator_words = ports.hooks().operator_words
    addresses, parse = ports.swarm().naming.addresses, ports.swarm().naming.parse

    names = addresses(by)
    codes = sorted({agent.code for agent in map(parse, names) if agent})
    crew = [n for code in codes for kind in ("master", "planner") for n in operator_words.recorded(f"{kind}@{code}-*")]
    return list(dict.fromkeys([*names, *crew, *watched_masters(by)]))


def verified(by, quote, within=None):
    """The quoted span, as the operator wrote it, of recorded words from any master or planner of the relaying agent's swarm."""
    operator_words = ports.hooks().operator_words

    if len(quote.split()) < 3:
        return ""
    words = next((w for name in speakers(by) if (w := operator_words.matching(name, quote, within=within))), "")
    found = re.search(r"\s+".join(map(re.escape, quote.split())), words, re.IGNORECASE)
    return found.group(0) if found else ""


def relayed(op, meta) -> str:
    """The operator's words an op quotes, when its author is the ledger's orchestrator and the hooks recorded them."""
    master = meta["members"].get(op["by"], {}).get("role") == "orchestrator"
    return verified(op["by"], op["quote"]) if master else ""


def orchestrator_words(op: dict[str, Any], ctx: Any) -> str:
    """The verified quoted span of an orchestrator's relay, or empty with the refusal recorded on ctx."""
    if ctx.meta["members"].get(op["by"], {}).get("role") != "orchestrator":
        ctx.refused.append(f"relay on {op['item']} refused: {op['by']} is not an orchestrator of this ledger")
        return ""
    words = verified(op["by"], op["quote"])
    if not words:
        ctx.refused.append(
            f"relay on {op['item']} refused: the server could not verify at least three quoted operator words "
            "in its recorded prompts or answers"
        )
    return words


def add_note(doc: dict[str, Any], op: dict[str, Any], ctx: Any) -> bool:
    """A new operator note carrying the orchestrator's relayed text and the operator's verified words."""
    if any(n["id"] == op["id"] for n in doc["notes"]):
        return True
    words = orchestrator_words(op, ctx)
    if not words:
        return False
    marks = {"relayed_by": op["by"], "relayed_from": RELAYED_FROM, "quote": words}
    text = op["text"].strip()
    doc["notes"].append({"id": op["id"], "by": "operator", "at": ctx.at, "text": text, "comments": [], **marks})
    ctx.record("operator", "note added", "", id=op["id"], text=text, **marks)
    ctx.stamp("notes", "operator")
    return True


def apply(doc: dict[str, Any], op: dict[str, Any], ctx: Any) -> bool:
    if op["item"] == "notes":
        return add_note(doc, op, ctx)
    name, item_id = op["item"].split("/")
    item = next((i for i in doc[name] if i["id"] == item_id), None)
    if item is None:
        ctx.refused.append(f"relay on {op['item']} refused: the entry does not exist")
        return False
    thread = "answers" if name == "questions" else "comments"
    if any(e["id"] == op["id"] for e in item[thread]):
        return True
    words = orchestrator_words(op, ctx)
    if not words:
        return False
    marks = {"relayed_by": op["by"], "relayed_from": RELAYED_FROM, "quote": words}
    text = op["text"].strip()
    item[thread].append({"id": op["id"], "by": "operator", "at": ctx.at, "text": text, **marks})
    noun = "answer" if thread == "answers" else "comment"
    ctx.record("operator", f"{noun} added", op["item"], id=op["id"], text=text, **marks)
    ctx.stamp(f"{op['item']}/{thread}", "operator")
    return True
