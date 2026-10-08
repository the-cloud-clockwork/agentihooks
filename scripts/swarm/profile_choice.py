"""The typed profile decision made before every swarm launch: explicit task profile, fixed lane, else the classifier."""

import os
from dataclasses import asdict, dataclass, replace

from hooks.classifier import Choice, ClassifierUnavailable, decide
from scripts.swarm import overlays
from scripts.swarm.templates import DEFAULT_PROFILES
from scripts.swarm_ledger import ledger_close

CLASSIFIED_LANE = "eng"
MIN_CONFIDENCE_VAR = "AGENTIHOOKS_PROFILE_PICK_MIN_CONFIDENCE"
MIN_CONFIDENCE = 0.6
RESPONSIBILITIES = {
    "frontend": (
        "A product interface behavior: what a person sees or does on a page, panel, control or layout, including a "
        "product interaction such as claim ordering or ranking from a view, even when Python code implements it."
    ),
    "engineer": (
        "Backend or infrastructure behavior: services, APIs, command lines, hooks, runtimes, storage, deployment or "
        "CI plumbing, with no change to what a person sees or does in a product interface."
    ),
    "qa": (
        "Independent verification: stress testing or proving work that others built, producing evidence rather "
        "than changing behavior."
    ),
}
NOT_ONE = {
    "split": "Two or more unrelated public responsibilities bundled in one task that should become separate tasks.",
    "unresolved": "The task does not say enough about the public behavior it changes to decide.",
}


class ProfileUnresolved(RuntimeError):
    pass


@dataclass(frozen=True)
class ProfileDecision:
    profile: str
    source: str
    responsibility: str
    model: str = ""
    confidence: float | None = None
    calibrated: bool | None = None
    anchors: tuple = ()
    overlays: tuple = ()
    bundle_revision: str = ""

    def record(self) -> dict:
        return {**asdict(self), "anchors": list(self.anchors), "overlays": list(self.overlays)}


def installed(name: str) -> bool:
    from scripts.targets._common import _install_module

    return _install_module()._resolve_profile_dir(name) is not None


def choose(
    slug: str, lane: str, lane_config: dict, task: dict, environ: dict, role_overlays: dict | None = None
) -> ProfileDecision:
    pinned = lane_config.get("profile") or DEFAULT_PROFILES[lane]
    if task.get("profile"):
        decision = ProfileDecision(task["profile"], "task", "explicit task profile")
    elif lane != CLASSIFIED_LANE or pinned != DEFAULT_PROFILES[lane]:
        decision = ProfileDecision(pinned, "lane", f"{lane} lane")
    else:
        decision = classify(slug, task, environ)
    if not installed(decision.profile):
        raise ProfileUnresolved(
            f"task {task.get('id')} needs profile {decision.profile}, which is not installed: install it with "
            f"agentihooks init or {_remedy(slug, task)}"
        )
    try:
        return replace(decision, overlays=overlays.chosen(decision.profile, task, role_overlays or {}))
    except ValueError as exc:
        raise ProfileUnresolved(f"task {task.get('id')} overlays are refused: {exc}") from exc


def classify(slug: str, task: dict, environ: dict) -> ProfileDecision:
    question = Choice(
        f'Task {task.get("id")} "{task.get("title", "")}": which responsibility owns the public behavior this task '
        "changes? Judge what a user or caller observes changing, from the description, parent intent and territory, "
        "never from keywords or file names. Mixed interface and backend work follows the public behavior changed.",
        {**RESPONSIBILITIES, **NOT_ONE},
    )
    try:
        result = decide(state(slug, task), {"responsibility": question}, purpose="profile-pick")
    except ClassifierUnavailable as exc:
        raise ProfileUnresolved(
            f"task {task.get('id')} profile classification is unavailable ({exc}): {_remedy(slug, task)}"
        ) from exc
    answer, floor = result.answers["responsibility"], float(environ.get(MIN_CONFIDENCE_VAR, MIN_CONFIDENCE))
    confidence = answer.confidence if answer.confidence is not None else 0.0
    if confidence < floor:
        return ProfileDecision(
            DEFAULT_PROFILES[CLASSIFIED_LANE],
            "lane default",
            f"{CLASSIFIED_LANE} lane default: {result.source} answered {answer.choice} "
            f"with confidence {confidence:.2f}, below the floor {floor:.2f}",
            result.source,
            answer.confidence,
            result.calibrated,
            anchors(task),
        )
    if answer.choice not in RESPONSIBILITIES:
        raise ProfileUnresolved(
            f"task {task.get('id')} profile is unresolved: {result.source} answered {answer.choice} with confidence "
            f"{confidence:.2f}, the floor is {floor:.2f}: {_remedy(slug, task)}"
        )
    return ProfileDecision(
        answer.choice, "classifier", answer.choice, result.source, answer.confidence, result.calibrated, anchors(task)
    )


def state(slug: str, task: dict) -> dict:
    doc = _ledger(slug)
    phases = (p for p in doc.get("phases", []) if p.get("id") == task.get("phase"))
    return {
        "task": task.get("id", ""),
        "title": task.get("title", ""),
        "description": task.get("description", ""),
        "kind": task.get("kind", "code"),
        "territory": list(task.get("territory", [])),
        "project_intent": doc.get("overview", "").partition(ledger_close.MARK)[0].strip(),
        "phase": task.get("phase", ""),
        "phase_intent": next((f"{p.get('title', '')}: {p.get('description', '')}" for p in phases), ""),
    }


def anchors(task: dict) -> tuple:
    found = [f"task:{task.get('id')}"] + ([f"phase:{task['phase']}"] if task.get("phase") else [])
    return tuple(found + [f"territory:{t}" for t in task.get("territory", [])])


def _ledger(slug: str) -> dict:
    from scripts.swarm_ledger.repository.folder import ledger_folder
    from scripts.swarm_ledger.repository.sqlite import read_ledger

    return read_ledger(ledger_folder(os.environ), slug, "overview", "phases") or {}


def _remedy(slug: str, task: dict) -> str:
    return (
        f"set it with agentihooks ledger --slug {slug} task set {task.get('id')} profile=<{'|'.join(RESPONSIBILITIES)}>"
        ", or split the task into one public responsibility each, then reopen it"
    )
