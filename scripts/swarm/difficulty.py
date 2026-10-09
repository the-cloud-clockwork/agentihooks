"""The tick step that gives every open task without a difficulty its size: a code rule first, else the classifier."""

import math
import re
from concurrent.futures import ThreadPoolExecutor

from hooks.classifier import ClassifierError, decide, definitions, runner
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm_ledger import ledger_kinds

PER_TICK = 5
FALLBACK = "M"
PURPOSE = "task-difficulty"
QUESTION = "difficulty"
PAGE_FOLDER = "scripts/swarm_ledger/static"
PAGE_FILES = (
    "scripts/swarm_ledger/home.html",
    "scripts/swarm_ledger/shell.html",
    "scripts/swarm_ledger/template.html",
    "scripts/swarm_ledger/palette.css",
    "scripts/swarm_ledger/tooltips.js",
)
INFRA_RE = re.compile(
    r"(?<![a-z0-9])(kubernetes|k8s|helm|argo|argocd|deploy(?:s|ing|ment|ments)?|infra(?:structure)?)(?![a-z0-9])",
    re.IGNORECASE,
)


def size_pass(slug: str, ledger, doc: dict) -> list[str]:
    unsized = [t for t in doc.get("tasks", []) if t.get("state") != "done" and not t.get("difficulty")][:PER_TICK]
    actions = []
    with ThreadPoolExecutor(max_workers=PER_TICK) as pool:
        for task, fields in zip(unsized, pool.map(lambda task: rule(task) or classify(task, doc), unsized)):
            try:
                ledger.update_task(slug, task["id"], fields)
            except LedgerRefused:
                actions.append(f"skipped sizing task {task['id']}: the ledger refused its write")
                continue
            actions.append(f"sized task {task['id']} {fields['difficulty']} by {fields['difficulty_source']}")
    return actions


def rule(task: dict) -> dict | None:
    territory = [str(area) for area in task.get("territory", [])]
    if ledger_kinds.kind(task) == "ops" or any(INFRA_RE.search(area) for area in territory):
        return sized("L", "rule", 1.0)
    if task.get("profile") == "frontend" and territory and all(on_page(area) for area in territory):
        return sized("S", "rule", 1.0)
    return None


def classify(task: dict, doc: dict) -> dict:
    params = {"id": task.get("id"), "title": task.get("title", "")}
    try:
        output = runner.run(PURPOSE, state(task, doc), params, decider=decide)
        answer = output.raw.answers[QUESTION]
    except ClassifierError:
        return sized(FALLBACK, "default", 0.0)
    raw = answer.confidence
    confidence = min(max(raw, 0.0), 1.0) if _real(raw) else 0.0
    options = output.definition.questions[0].question.options
    if confidence < output.thresholds["confidence"] or answer.choice not in options:
        return sized(FALLBACK, "default", confidence)
    return sized(answer.choice, "classifier", confidence)


def state(task: dict, doc: dict) -> dict:
    phase = {p["id"]: p for p in doc.get("phases", []) if p.get("id")}.get(task.get("phase"))
    return {
        "task": task.get("id", ""),
        "title": task.get("title", ""),
        "description": task.get("description", ""),
        "kind": ledger_kinds.kind(task),
        "profile": task.get("profile", ""),
        "territory": list(task.get("territory", [])),
        "phase_intent": f"{phase.get('title', '')}: {phase.get('description', '')}" if phase else "",
    }


def sized(difficulty: str, source: str, confidence: float) -> dict:
    return {"difficulty": difficulty, "difficulty_source": source, "difficulty_confidence": confidence}


def _real(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isnan(value)


def on_page(area):
    return area in PAGE_FILES or area == PAGE_FOLDER or area.startswith(f"{PAGE_FOLDER}/")


def __getattr__(name):
    if name == "MIN_CONFIDENCE":
        return definitions.load(PURPOSE).thresholds["confidence"]
    raise AttributeError(name)
