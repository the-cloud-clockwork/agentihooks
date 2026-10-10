"""Finds the ledger item a new task, follow up or phase repeats, so an add can name it instead of landing twice.

Code shortlists candidates by word overlap; the classifier only confirms each new item and candidate pair.
"""

import json
import math
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass

from hooks.classifier import ClassifierError, code_rules, decide, runner
from scripts.swarm_ledger import ledger_rank

PURPOSE = "ledger-duplicate"
SHORTLIST = 4
UNCHECKED = "unchecked"
LISTS = {"task": "tasks", "followup": "followups", "phase": "phases"}
WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset(
    "the and for with that this from into when then than are was were has have not but its their them they who "
    "what which each every one all any can will should must does task follow phase add new".split()
)


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


def find(doc: dict, kind: str, items: list[dict], judge: Callable | None = None) -> list[Match | str | None]:
    pool = _pool(doc, kind)
    shortlists = [shortlist(item, pool) for item in items]
    if not any(shortlists):
        return [None] * len(items)
    pairs = [
        {"new": i, "slot": j, "kind": c[0], "id": c[1]["id"], "title": _title(c[1])}
        for i, found in enumerate(shortlists)
        for j, c in enumerate(found)
    ]
    try:
        output = runner.run(PURPOSE, _state(items, shortlists), {"pairs": pairs}, decider=judge or decide)
    except ClassifierError:
        return [UNCHECKED if found else None for found in shortlists]
    floor = output.thresholds["same"]
    return [_best(doc, i, found, output.raw.answers, floor) for i, found in enumerate(shortlists)]


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


def _best(doc, i, found, answers, floor):
    yes = [(answers[_name(i, j)].noul, c) for j, c in enumerate(found)]
    yes = [(p, c) for p, c in yes if _yes(p, floor)]
    if not yes:
        return None
    probability, (kind, item) = max(yes, key=lambda y: y[0])
    return _match(doc, kind, item, probability)


def _yes(noul, floor):
    return isinstance(noul, (int, float)) and not isinstance(noul, bool) and noul > floor


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


def _verdicts(definition, state, params, answers):
    floor = definition.thresholds["same"]
    return {"duplicate": any(_yes(answers[_name(p["new"], p["slot"])].noul, floor) for p in params["pairs"])}


RULE = code_rules.CodeRule(code_rules.asked, _verdicts, {"duplicate": (True, False)}, {"duplicate": False})


def main() -> None:
    request = json.load(sys.stdin)
    found = find(request["doc"], request["kind"], request["items"])
    print(json.dumps([asdict(match) if isinstance(match, Match) else match for match in found]))


if __name__ == "__main__":
    main()
