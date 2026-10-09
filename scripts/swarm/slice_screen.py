"""The classifier pre-screen of a slice: advisory flags for a task that may be too big or off the phase intent.

Flags never send a slice back. At full autonomy the tick approves a slice only when `hold` returns nothing.
"""

from dataclasses import dataclass

from hooks.classifier import ClassifierError, decide, definitions, runner
from scripts.swarm import slice_check
from scripts.swarm_ledger import ledger_kinds

PURPOSE = "phase-slice"
ONE_PR = 1


@dataclass(frozen=True)
class Screen:
    flags: tuple = ()
    reason: str = ""


def slice_tasks(phase, doc):
    known = {t["id"]: t for t in doc["tasks"]}
    ids = slice_check.slice_ids(slice_check.plan_task(phase, doc))
    return [known[tid] for tid in ids if known.get(tid, {}).get("phase") == phase["id"]]


def screen(phase, doc, confidence):
    mine = slice_tasks(phase, doc)
    state = {
        "phase": phase["title"],
        "intent": phase["description"],
        "overview": doc["overview"],
        "tasks": [
            {
                "id": t["id"],
                "title": t["title"],
                "description": t["description"],
                "kind": ledger_kinds.kind(t),
                "territory": t.get("territory") or [],
            }
            for t in mine
        ],
    }
    try:
        result = runner.run(PURPOSE, state, {"tasks": mine}, decider=decide).raw
    except ClassifierError:
        return Screen(reason="the classifier did not answer")
    reason = "" if result.calibrated else f"the answer came from the fallback {result.source}"
    return Screen(tuple(flags(result.answers, mine, confidence)), reason)


def levels(definition):
    return next(spec.question.levels for spec in definition.questions if spec.name == "size")


def flags(answers, mine, confidence):
    definition = definitions.load(PURPOSE)
    sizes, off_intent = levels(definition), definition.thresholds["off_intent"]
    found = []
    for i, task in enumerate(mine):
        size, serves = answers[f"size_{i}"], answers[f"serves_{i}"]
        level = round(size.score)
        if level > ONE_PR and size.confidence >= confidence:
            found.append(
                f"Classifier: task {task['id']} may be too big, {sizes[level]} at confidence {size.confidence:.2f}."
            )
        if serves.noul < off_intent:
            found.append(
                f"Classifier: task {task['id']} may be off intent, serves the phase at probability {serves.noul:.2f}."
            )
    return found


def hold(problems, result):
    if problems:
        return "the slice check found problems"
    if result.flags:
        return "the classifier flagged a task"
    return result.reason


def __getattr__(name):
    if name == "SIZES":
        return levels(definitions.load(PURPOSE))
    raise AttributeError(name)
