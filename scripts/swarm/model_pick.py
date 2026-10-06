from dataclasses import dataclass

from hooks.classifier import Choice, ClassifierUnavailable, Score, decide

MODEL_TIERS = {
    "claude": {"small": "sonnet", "medium": "opus", "large": "opus"},
    "codex": {"small": "gpt-6-luna", "medium": "gpt-6.1-sol", "large": "gpt-6.1-sol"},
}
EFFORTS = {"claude": ("low", "medium", "high", "max"), "codex": ("low", "medium", "high", "xhigh")}


@dataclass(frozen=True)
class ModelPick:
    model: str
    effort: str
    source: str = "lane-default"
    confidence: float | None = None


def tier_models(harness: str, environ: dict) -> dict:
    models = dict(MODEL_TIERS[harness])
    raw = environ.get(f"AGENTIHOOKS_MODEL_TIERS_{harness.upper()}", "")
    if not raw:
        return models
    for entry in raw.split(","):
        tier, separator, model = entry.strip().partition("=")
        if not separator or tier.strip() not in models or not model.strip():
            raise ValueError("model tiers must be small=model,medium=model,large=model")
        models[tier.strip()] = model.strip()
    return models


def pick(harness: str, lane: dict, task: dict, environ: dict) -> ModelPick:
    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    questions = {}
    if default.model == "auto":
        questions["tier"] = Choice(
            "Which model tier fits this task?",
            {
                "small": "Routine, narrowly scoped task with a clear solution",
                "medium": "Task requiring analysis across several components",
                "large": "Complex architecture or uncertain system design",
            },
        )
    if default.effort == "auto":
        questions["effort"] = Score("How much reasoning does this task need?", list(EFFORTS[harness]))
    if not questions:
        return default
    try:
        result = decide(
            {
                "title": task.get("title", ""),
                "description": task.get("description", ""),
                "kind": task.get("kind", "code"),
                "territory_size": len(task.get("territory", [])),
            },
            questions,
            purpose="model-pick",
            harness=harness,
        )
    except ClassifierUnavailable:
        return default
    confidence = min(result.answers[key].confidence for key in questions)
    if confidence < float(environ.get("AGENTIHOOKS_MODEL_PICK_MIN_CONFIDENCE", "0.6")):
        return ModelPick(default.model, default.effort, result.source, confidence)
    return ModelPick(
        tier_models(harness, environ)[result.answers["tier"].choice] if default.model == "auto" else default.model,
        EFFORTS[harness][round(max(0, min(3, result.answers["effort"].score)))]
        if default.effort == "auto"
        else default.effort,
        result.source,
        confidence,
    )
