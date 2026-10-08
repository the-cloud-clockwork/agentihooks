"""The tick step that gives every open task without a difficulty its size: a code rule first, else the classifier."""

import re

from hooks.classifier import Choice, ClassifierUnavailable, decide

PER_TICK = 5
MIN_CONFIDENCE = 0.6
FALLBACK = "M"
QUESTION = "difficulty"
PAGE_FOLDER = "scripts/swarm_ledger/static"
INFRA_RE = re.compile(r"kubernetes|k8s|helm|argo|deploy|infra", re.IGNORECASE)
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


def size_pass(slug, ledger, doc, environ):
    unsized = [t for t in doc.get("tasks", []) if t.get("state", "open") != "done" and not t.get("difficulty")]
    actions = []
    for task in unsized[: int(environ.get("AGENTIHOOKS_DIFFICULTY_PER_TICK", PER_TICK))]:
        fields = rule(task) or classify(task, doc, environ)
        ledger.update_task(slug, task["id"], fields)
        actions.append(f"sized task {task['id']} {fields['difficulty']} by {fields['difficulty_source']}")
    return actions


def rule(task):
    territory = [str(area) for area in task.get("territory", [])]
    if task.get("kind") == "ops" or any(INFRA_RE.search(area) for area in territory):
        return sized("L", "rule", 1.0)
    if task.get("profile") == "frontend" and territory and all(_on_page(area) for area in territory):
        return sized("S", "rule", 1.0)
    return None


def classify(task, doc, environ):
    question = Choice(INSTRUCTIONS.format(id=task.get("id"), title=task.get("title", "")), RUBRIC)
    try:
        answer = decide(state(task, doc), {QUESTION: question}, purpose="task-difficulty").answers[QUESTION]
    except ClassifierUnavailable:
        return sized(FALLBACK, "default", 0.0)
    confidence = answer.confidence or 0.0
    floor = float(environ.get("AGENTIHOOKS_DIFFICULTY_MIN_CONFIDENCE", MIN_CONFIDENCE))
    if confidence < floor or answer.choice not in RUBRIC:
        return sized(FALLBACK, "default", confidence)
    return sized(answer.choice, "classifier", confidence)


def state(task, doc):
    phase = next((p for p in doc.get("phases", []) if p.get("id") == task.get("phase")), {})
    return {
        "task": task.get("id", ""),
        "title": task.get("title", ""),
        "description": task.get("description", ""),
        "kind": task.get("kind", "code"),
        "profile": task.get("profile", ""),
        "territory": list(task.get("territory", [])),
        "phase_intent": f"{phase.get('title', '')}: {phase.get('description', '')}" if phase else "",
    }


def sized(difficulty, source, confidence):
    return {"difficulty": difficulty, "difficulty_source": source, "difficulty_confidence": confidence}


def _on_page(area):
    return area.rstrip("/") == PAGE_FOLDER or area.startswith(f"{PAGE_FOLDER}/")
