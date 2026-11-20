"""The typed profile decision made before every swarm launch: explicit task profile, fixed lane, else the classifier."""

from collections.abc import Collection
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any

from hooks.classifier import ClassifierUnavailable, decide, definitions, runner
from scripts.swarm import overlays
from scripts.swarm.templates import DEFAULT_PROFILES
from scripts.swarm_ledger import ledger_close

if TYPE_CHECKING:
    from scripts.swarm.store import SwarmConfig

CLASSIFIED_LANE = "eng"
PURPOSE = "profile-pick"
RESPONSIBILITIES = ("frontend", "engineer", "qa")
HARNESS_ORDER = {"frontend": ("claude", "codex")}
FRESH = "fresh_spawn"
LEDGER_INTENT = "ledger_intent"


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


def preferred(profile: str) -> tuple[str, ...]:
    return HARNESS_ORDER.get(profile, ())


def choose(
    config: "SwarmConfig", lane: str, task: dict, environ: dict, hosted: Collection[str] = ()
) -> ProfileDecision:
    """Decide the launch profile; it counts as installed when this host resolves it or a placement target hosts it."""
    decision = pick(config.slug, lane, config.lanes.get(lane, {}), task, environ)
    if decision.profile not in hosted and not installed(decision.profile):
        raise ProfileUnresolved(
            f"task {task.get('id')} needs profile {decision.profile}, which is not installed: install it with "
            f"agentihooks init or {_remedy(config.slug, task)}"
        )
    try:
        return replace(
            decision, overlays=overlays.chosen(decision.profile, task, getattr(config, "overlays", {}), hosted)
        )
    except ValueError as exc:
        raise ProfileUnresolved(f"task {task.get('id')} overlays are refused: {exc}") from exc


def pick(slug: str, lane: str, lane_config: dict, task: dict, environ: dict | None = None) -> ProfileDecision:
    pinned = lane_config.get("profile") or DEFAULT_PROFILES[lane]
    if task.get("profile"):
        return ProfileDecision(task["profile"], "task", "explicit task profile")
    if saved := task.get("profile_decision"):
        return ProfileDecision(**{**saved, "anchors": tuple(saved["anchors"]), "overlays": tuple(saved["overlays"])})
    if lane != CLASSIFIED_LANE or pinned != DEFAULT_PROFILES[lane]:
        return ProfileDecision(pinned, "lane", f"{lane} lane")
    if needs_ci_push(task):
        return ProfileDecision(
            DEFAULT_PROFILES[CLASSIFIED_LANE], "proof contract", "proof needs a pushed CI run", anchors=anchors(task)
        )
    return classify(slug, task, environ)


def kept(task: dict, saved: dict, relaunch: dict) -> ProfileDecision | None:
    if not saved.get("profile") or (task.get("profile") and not relaunch):
        return None
    return ProfileDecision(
        saved["profile"], "handoff", "original seat profile", overlays=tuple(saved.get("overlays", ()))
    )


def transfer(task: dict) -> dict:
    from scripts.swarm.tick import SpawnError

    saved = (task.get("handoff_envelope") or {}).get("launch") or {}
    if task.get(FRESH) or not (saved.get("profile") and saved.get("harness")):
        return {}
    if saved["harness"] not in ("claude", "codex"):
        raise SpawnError(f"unsupported handoff harness: {saved['harness']}", "unsupported")
    return saved


def needs_ci_push(task: dict) -> bool:
    return (task.get("contract") or {}).get("push") == "yes"


def classify(slug: str, task: dict, environ: dict) -> ProfileDecision:
    params = {"id": task.get("id"), "title": task.get("title", "")}
    try:
        output = runner.run(PURPOSE, state(slug, task), params, decider=decide, environ=environ)
    except ClassifierUnavailable as exc:
        return ProfileDecision(
            DEFAULT_PROFILES[CLASSIFIED_LANE],
            "classifier unavailable",
            f"{CLASSIFIED_LANE} lane default: profile classification is unavailable ({exc})",
            anchors=anchors(task),
        )
    except definitions.DefinitionError as exc:
        raise ProfileUnresolved(
            f"task {task.get('id')} profile classification is unavailable ({exc}): {_remedy(slug, task)}"
        ) from exc
    result, floor = output.raw, output.thresholds["confidence"]
    answer = result.answers["responsibility"]
    confidence = answer.confidence if answer.confidence is not None else 0.0
    if output.verdicts["responsibility"] is None:
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
    doc = task[LEDGER_INTENT] if LEDGER_INTENT in task else _ledger(slug)
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


def carried(doc: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """The project intent and the task's own phase a remote hive classifies with, since it has no ledger identity."""
    phases: list[dict[str, Any]] = doc.get("phases", [])
    return {"overview": doc.get("overview", ""), "phases": [p for p in phases if p.get("id") == task.get("phase")]}


def anchors(task: dict) -> tuple:
    found = [f"task:{task.get('id')}"] + ([f"phase:{task['phase']}"] if task.get("phase") else [])
    return tuple(found + [f"territory:{t}" for t in task.get("territory", [])])


def _ledger(slug: str) -> dict:
    from scripts.swarm import ledger_read

    return ledger_read.parts(slug, "overview", "phases")


def _remedy(slug: str, task: dict) -> str:
    return (
        f"set it with agentihooks ledger --slug {slug} task set {task.get('id')} profile=<{'|'.join(RESPONSIBILITIES)}>"
        ", or split the task into one public responsibility each, then reopen it"
    )
