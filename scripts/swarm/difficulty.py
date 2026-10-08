"""The tick step that gives every open task without a difficulty its size: a code rule first, else the classifier."""

import math
import re
from concurrent.futures import ThreadPoolExecutor

from hooks.classifier import Choice, ClassifierError, decide
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm_ledger import ledger_kinds

PER_TICK = 5
MIN_CONFIDENCE = 0.6
FALLBACK = "M"
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
RUBRIC = {
    "S": "A mundane frontend change: copy, layout, style or a small control on a page, even when grouped with others.",
    "M": "Normal agentihooks code or CI work: hooks, scripts, commands, the ledger server, tests or workflows.",
    "L": (
        "Touches infrastructure, collects information, sets configuration or deploys to Kubernetes, possibly "
        "together with code."
    ),
}
INSTRUCTIONS = (
    'Task {id} "{title}": how big is this task by the operator rubric? Judge the work it asks for from the '
    "description, kind, profile, territory and phase intent."
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
    question = Choice(INSTRUCTIONS.format(id=task.get("id"), title=task.get("title", "")), RUBRIC)
    try:
        answer = decide(state(task, doc), {QUESTION: question}, purpose="task-difficulty").answers[QUESTION]
    except ClassifierError:
        return sized(FALLBACK, "default", 0.0)
    raw = answer.confidence
    confidence = min(max(raw, 0.0), 1.0) if _real(raw) else 0.0
    if confidence < MIN_CONFIDENCE or answer.choice not in RUBRIC:
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
