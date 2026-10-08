"""Finds the ledger item a new task, follow up or phase repeats, so an add can name it instead of landing twice.

Code shortlists candidates by word overlap; the classifier only confirms each new item and candidate pair.
"""

import math
import re
from dataclasses import dataclass

from hooks.classifier import ClassifierError, YesNo, decide
from scripts.swarm_ledger import ledger_rank

PURPOSE = "ledger-duplicate"
SHORTLIST = 4
YES = 0.6
UNCHECKED = "unchecked"
LISTS = {"task": "tasks", "followup": "followups", "phase": "phases"}
WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset(
    "the and for with that this from into when then than are was were has have not but its their them they who "
    "what which each every one all any can will should must does task follow phase add new".split()
)
SAME = "Does new item {new} ask for the same change as {kind} {id} titled {title}?"
TRUE = "the new item asks for the same change as the existing item"
FALSE = "the new item asks for a different change"


@dataclass(frozen=True)
class Match:
    id: str
    kind: str
    title: str
    state: str
    rank: str | None
    phase: str | None
    phase_title: str | None
    probability: float

    @property
    def built(self) -> bool:
        return self.state == "done"


def find(doc: dict, kind: str, items: list[dict], judge=None) -> list:
    pool = _pool(doc, kind)
    shortlists = [shortlist(item, pool) for item in items]
    if not any(shortlists):
        return [None] * len(items)
    questions = {
        _name(i, j): YesNo(SAME.format(new=i, kind=c[0], id=c[1]["id"], title=_title(c[1])), true=TRUE, false=FALSE)
        for i, found in enumerate(shortlists)
        for j, c in enumerate(found)
    }
    try:
        answers = (judge or decide)(_state(items, shortlists), questions, purpose=PURPOSE).answers
    except ClassifierError:
        return [UNCHECKED if found else None for found in shortlists]
    return [_best(doc, i, found, answers) for i, found in enumerate(shortlists)]


def shortlist(item: dict, pool: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    mine = words(item)
    scored = [(_overlap(mine, words(c[1])), n, c) for n, c in enumerate(pool)]
    ranked = sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1]))
    return [c for _, _, c in ranked[:SHORTLIST]]


def words(item: dict) -> set[str]:
    text = " ".join(str(item.get(k) or "") for k in ("title", "description", "text")).lower()
    return {w for w in WORD.findall(text) if len(w) > 2 and w not in STOP}


def _pool(doc, kind):
    pool = [(kind, i) for i in doc.get(LISTS[kind], []) if not i.get("out_of_scope")]
    if kind == "followup":
        pool += [("task", t) for t in doc.get("tasks", []) if t.get("state") != "done" and not t.get("out_of_scope")]
    return pool


def _overlap(mine, theirs):
    if not mine or not theirs:
        return 0
    return len(mine & theirs) / math.sqrt(len(mine) * len(theirs))


def _state(items, shortlists):
    return {
        "new": [
            {"ref": i, "title": _title(item), "description": item.get("description", "")}
            for i, item in enumerate(items)
        ],
        "existing": [
            {"kind": c[0], "id": c[1]["id"], "title": _title(c[1]), "description": c[1].get("description", "")}
            for found in shortlists
            for c in found
        ],
    }


def _best(doc, i, found, answers):
    yes = [(answers[_name(i, j)].noul, c) for j, c in enumerate(found)]
    yes = [(p, c) for p, c in yes if isinstance(p, (int, float)) and not isinstance(p, bool) and p > YES]
    if not yes:
        return None
    probability, (kind, item) = max(yes, key=lambda y: y[0])
    return _match(doc, kind, item, probability)


def _match(doc, kind, item, probability):
    if kind != "task":
        return Match(item["id"], kind, _title(item), _done(item), None, None, None, probability)
    phase = next((p for p in doc.get("phases", []) if p["id"] == item.get("phase")), {})
    rank = item.get("rank", ledger_rank.DEFAULT)
    return Match(
        item["id"], kind, item["title"], item["state"], rank, item.get("phase"), phase.get("title"), probability
    )


def _done(item):
    return "done" if item.get("done") else "open"


def _title(item):
    return item.get("title") or item.get("text", "")


def _name(i, j):
    return f"new_{i}_existing_{j}"
